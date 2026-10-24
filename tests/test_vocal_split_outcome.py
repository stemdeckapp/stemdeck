"""What a failed Lead + Backing split leaves behind (#832).

The split is a second pass over a finished job's vocals. When it failed, the
song kept one Vocals stem, the request answered a bare 500, and the job state
said only vocal_split "error": nothing a person could read, so the client
carried on as if Lead + Backing had never been asked for.

Each failure class runs the real worker (app/pipeline/vocal_split_worker.py)
through the real endpoint, with only the step that fails replaced, so the
code the worker reports is the one the parent records.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import registry
from app.core.models import Job
from app.core.registry import _jobs

JOB_ID = "abcdef832832"
# A path and a traceback-shaped line that must never reach the client.
SECRET_PATH = "/home/someone/StemDeck/models/audio-separator/UVR_MDXNET_KARA_2.onnx"


@pytest.fixture(autouse=True)
def _fresh_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def done_job(tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    stems_dir = tmp_path / JOB_ID / "stems"
    stems_dir.mkdir(parents=True)
    (stems_dir / "vocals.wav").write_bytes(b"RIFF1234")
    job = Job(
        id=JOB_ID,
        status="done",
        title="Song",
        stems=[{"name": "vocals", "url": f"/files/{JOB_ID}/stems/vocals.wav"}],
    )
    _jobs[job.id] = job
    return job, stems_dir


def _worker(body: str):
    """A spawn seam that runs the real worker with one step replaced.

    `body` is Python run inside the child before main(): it patches the
    worker module `w` so one phase raises as the real failure would."""
    code = f"import sys\nimport app.pipeline.vocal_split_worker as w\n{body}\nw.main()\n"

    def spawn(device: str, vocals_path: Path, out_dir: Path) -> list[str]:
        return [sys.executable, "-c", code, device, str(vocals_path), str(out_dir)]

    return spawn


def _raw(code: str):
    def spawn(device: str, vocals_path: Path, out_dir: Path) -> list[str]:
        return [sys.executable, "-c", code, device, str(vocals_path), str(out_dir)]

    return spawn


def _use(monkeypatch, spawn):
    from app.pipeline import vocal_split as vs_mod

    monkeypatch.setattr(vs_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(vs_mod, "_spawn_cmd", spawn)


IMPORT_FAILS = (
    "def boom():\n"
    "    raise ImportError(\"No module named 'torchvision' (" + SECRET_PATH + ')")\n'
    "w._import_separator = boom"
)
SHARED_LIB_FAILS = (
    "def boom():\n"
    "    raise OSError('libcudart.so.12: cannot open shared object file: No such file')\n"
    "w._import_separator = boom"
)
NMS_FAILS = (
    "class S:\n"
    "    def __init__(self, **kw): pass\n"
    "w._import_separator = lambda: S\n"
    "def boom(separator):\n"
    "    raise RuntimeError('operator torchvision::nms does not exist')\n"
    "w._load = boom"
)
DOWNLOAD_FAILS = (
    "class S:\n"
    "    def __init__(self, **kw): pass\n"
    "w._import_separator = lambda: S\n"
    "def boom(separator):\n"
    "    raise RuntimeError('Failed to download file from https://example.invalid/"
    "UVR_MDXNET_KARA_2.onnx, response code: 404')\n"
    "w._load = boom"
)
OFFLINE_FAILS = (
    "class S:\n"
    "    def __init__(self, **kw): pass\n"
    "w._import_separator = lambda: S\n"
    "def boom(separator):\n"
    "    raise ConnectionError('Max retries exceeded with url: /UVR_MDXNET_KARA_2.onnx')\n"
    "w._load = boom"
)
OOM_FAILS = (
    "class S:\n"
    "    def __init__(self, **kw): pass\n"
    "w._import_separator = lambda: S\n"
    "w._load = lambda separator: None\n"
    "def boom(separator, path):\n"
    "    raise MemoryError()\n"
    "w._separate = boom"
)
SEPARATE_FAILS = (
    "class S:\n"
    "    def __init__(self, **kw): pass\n"
    "w._import_separator = lambda: S\n"
    "w._load = lambda separator: None\n"
    "def boom(separator, path):\n"
    "    raise ValueError('bad shape in " + SECRET_PATH + "')\n"
    "w._separate = boom"
)
# Dies without the protocol's last line: a segfault, the OOM killer.
CRASHES = "import os\nos._exit(139)"


@pytest.mark.parametrize(
    ("spawn", "code"),
    [
        (_worker(IMPORT_FAILS), "dependency_missing"),
        (_worker(SHARED_LIB_FAILS), "dependency_missing"),
        (_worker(NMS_FAILS), "dependency_missing"),
        (_worker(DOWNLOAD_FAILS), "model_unavailable"),
        (_worker(OFFLINE_FAILS), "model_unavailable"),
        (_worker(OOM_FAILS), "out_of_memory"),
        (_worker(SEPARATE_FAILS), "worker_failed"),
        (_raw(CRASHES), "worker_failed"),
    ],
    ids=[
        "torchvision-import",
        "shared-library",
        "torchvision-op",
        "model-download",
        "offline",
        "out-of-memory",
        "separate-raises",
        "worker-crash",
    ],
)
def test_a_failed_split_keeps_the_song_and_says_why(client, done_job, monkeypatch, spawn, code):
    job, stems_dir = done_job
    _use(monkeypatch, spawn)

    r = client.post(f"/api/jobs/{job.id}/vocal-split")

    # The song is never failed over its split.
    assert r.status_code == 500
    assert job.status == "done"
    assert (stems_dir / "vocals.wav").read_bytes() == b"RIFF1234"
    assert not (stems_dir / "lead_vocals.wav").exists()

    state = client.get(f"/api/jobs/{job.id}").json()
    assert state["status"] == "done"
    assert [s["name"] for s in state["stems"]] == ["vocals"]
    assert state["vocal_split"] == "error"
    # Machine-readable, and the same in the failed request's own body.
    assert state["vocal_split_reason"] == code
    assert r.json()["vocal_split_reason"] == code
    message = state["vocal_split_message"]
    assert isinstance(message, str) and message
    # Nothing internal reaches the client: no path, no traceback, no stderr.
    for text in (r.text, str(state)):
        assert SECRET_PATH not in text
        assert "Traceback" not in text
        assert "/home/" not in text
    # The evidence stays on the server, where the logs and the report go.
    assert (stems_dir / "vocal_split_error.txt").is_file()


def test_the_reason_survives_a_restart(client, done_job, monkeypatch, tmp_path):
    job, _ = done_job
    _use(monkeypatch, _worker(IMPORT_FAILS))
    client.post(f"/api/jobs/{job.id}/vocal-split")

    restored = Job.from_record(job.to_record())
    assert restored.vocal_split == "error"
    assert restored.vocal_split_reason == "dependency_missing"


def test_a_split_cut_off_by_a_restart_can_run_again():
    """A record saved while the split ran comes back failed, not running:
    "running" refused every later attempt with a 409 for good."""
    job = Job(id=JOB_ID, status="done", title="Song", vocal_split="running")
    restored = Job.from_record(job.to_record())
    assert restored.vocal_split == "error"
    assert restored.vocal_split_reason == "interrupted"


def test_a_stalled_split_reports_a_timeout(client, done_job, monkeypatch):
    from app.pipeline import vocal_split as vs_mod

    job, _ = done_job
    monkeypatch.setattr(vs_mod, "TIMEOUT_VOCAL_SPLIT", 0.5)
    monkeypatch.setattr(vs_mod, "VOCAL_SPLIT_WATCHDOG_POLL_SEC", 0.1)
    _use(monkeypatch, _raw("import time\ntime.sleep(30)"))

    r = client.post(f"/api/jobs/{job.id}/vocal-split")

    assert r.status_code == 500
    assert job.vocal_split_reason == "timeout"


def test_a_cancelled_split_says_cancelled(client, done_job, monkeypatch):
    job, _ = done_job
    _use(monkeypatch, _raw("import time\ntime.sleep(30)"))
    result = {}

    def run():
        result["r"] = client.post(f"/api/jobs/{job.id}/vocal-split")

    t = threading.Thread(target=run)
    t.start()
    deadline = time.monotonic() + 20
    while registry.get_proc(job.id) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert registry.get_proc(job.id) is not None
    client.post(f"/api/jobs/{job.id}/cancel")
    t.join(timeout=20)

    assert result["r"].status_code == 500
    assert job.status == "done"
    assert job.vocal_split == "error"
    assert job.vocal_split_reason == "cancelled"
    # The cancel was for that run only: the next one is not cancelled by it.
    assert job.cancel_requested is False


SUCCESS = (
    "import os, sys\n"
    "out = sys.argv[3]\n"
    "open(os.path.join(out, 'lead_vocals.wav'), 'wb').write(b'RIFF')\n"
    "open(os.path.join(out, 'backing_vocals.wav'), 'wb').write(b'RIFF')\n"
    "sys.stderr.write('@@DONE@@\\n')\n"
)


def test_try_again_after_the_cause_is_fixed(client, done_job, monkeypatch):
    """Recovery without re-importing: the same endpoint runs the split again on
    the same song, and a success clears the reason."""
    job, stems_dir = done_job
    _use(monkeypatch, _worker(IMPORT_FAILS))
    assert client.post(f"/api/jobs/{job.id}/vocal-split").status_code == 500
    assert job.vocal_split_reason == "dependency_missing"

    _use(monkeypatch, _raw(SUCCESS))
    r = client.post(f"/api/jobs/{job.id}/vocal-split")

    assert r.status_code == 200
    body = r.json()
    assert body["vocal_split"] == "done"
    assert body["vocal_split_reason"] is None
    assert body["vocal_split_message"] is None
    assert {"lead_vocals", "backing_vocals"} <= {s["name"] for s in body["stems"]}


def test_an_older_worker_message_is_still_classified(client, done_job, monkeypatch):
    """The plain-string error line the worker used to write: read for its words."""
    job, _ = done_job
    _use(
        monkeypatch,
        _raw(
            "import sys, json\n"
            "sys.stderr.write('@@ERROR@@' + json.dumps(\"No module named 'onnx2torch'\") + '\\n')\n"
            "sys.exit(1)\n"
        ),
    )
    client.post(f"/api/jobs/{job.id}/vocal-split")
    assert job.vocal_split_reason == "dependency_missing"
