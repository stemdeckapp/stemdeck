"""Tests for the persistent demucs worker (#309) and the GPU->CPU separation
fallback (#276).

The worker invocation is swapped for stub Python scripts via the
_spawn_worker_cmd seam, so the real process machinery (Popen, stdin
dispatch, stderr streaming, watchdog, cancel translation, worker reuse) runs
end-to-end without demucs or a GPU.
"""

from __future__ import annotations

import contextlib
import socket
import sys
import threading
from pathlib import Path

import pytest

from app.core.models import Job, JobCancelled
from app.pipeline import separate as sep_mod
from app.pipeline.errors import SeparationError, classify_failure

# A persistent worker stub: says its model is loaded, then reads one JSON job
# request per line for as long as stdin stays open, always succeeding (writes
# a stem WAV where the real worker would, then "100%" + "@@DONE@@" to stderr)
# and keeps serving.
_SUCCESS_WORKER = """
import sys, json, os
sys.stderr.write("@@READY@@\\n")
for line in sys.stdin:
    req = json.loads(line)
    d = os.path.join(req["job_dir"], "htdemucs_6s", "source")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "vocals.wav"), "wb").write(b"RIFF")
    sys.stderr.write("100%\\n@@DONE@@\\n")
    sys.stderr.flush()
"""

# A worker stub that fails its first (only) dispatched job with a CUDA-OOM
# -shaped message, then exits -- matching demucs_worker.py's real behavior of
# never trying to keep serving after a failure.
_FAILING_WORKER = """
import sys, json
sys.stdin.readline()
sys.stderr.write("@@ERROR@@" + json.dumps("CUDA out of memory. Tried 2 GiB") + "\\n")
sys.stderr.flush()
sys.exit(1)
"""


def _stub_spawns(fail_devices: set[str], calls: list[str]):
    """A _spawn_worker_cmd replacement. `calls` records one entry per SPAWNED
    worker process (not per dispatched job) -- reuse across jobs on the same
    device means fewer calls than jobs, which the reuse tests assert on."""

    def fake_spawn(device: str) -> list[str]:
        calls.append(device)
        code = _FAILING_WORKER if device in fail_devices else _SUCCESS_WORKER
        return [sys.executable, "-c", code]

    return fake_spawn


@pytest.fixture()
def job(tmp_path: Path):
    j = Job(id="abcdefabc276")
    (tmp_path / "source.wav").write_bytes(b"RIFF")
    return j


@pytest.fixture(autouse=True)
def _reset_worker():
    """Each test starts and ends with no lingering worker reference -- a
    prior test's stub process must never leak into the next test."""
    sep_mod._worker.clear()
    yield
    sep_mod._kill_worker()


def test_gpu_failure_falls_back_to_cpu(job, tmp_path, monkeypatch, caplog):
    import logging

    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", _stub_spawns({"cuda"}, calls))

    with caplog.at_level(logging.WARNING, logger="stemdeck.pipeline"):
        stems_root = sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert calls == ["cuda", "cpu"]
    assert (stems_root / "vocals.wav").is_file()
    assert job.gpu_fallback is True
    assert job.compute_device == "cpu (fallback from cuda)"
    # Loud, never silent: the warning names device, cause, and stderr.
    warning = next(r.message for r in caplog.records if "retrying on CPU" in r.message)
    assert "cause=out-of-memory" in warning
    assert "CUDA out of memory" in warning


def test_dispatch_omits_extra_shifts_at_standard_quality(monkeypatch, tmp_path):
    monkeypatch.setattr(sep_mod, "get_separation_quality", lambda: "standard")
    # Drive a real job and have the stub echo the dispatched request's
    # "shifts" value back via a marker file -- simplest way to inspect what
    # separate() actually sent without patching json.dumps at the call site.
    echo_worker = """
import sys, json, os
for line in sys.stdin:
    req = json.loads(line)
    d = os.path.join(req["job_dir"], "htdemucs_6s", "source")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "vocals.wav"), "wb").write(b"RIFF")
    open(os.path.join(req["job_dir"], "shifts.txt"), "w").write(str(req["shifts"]))
    sys.stderr.write("100%\\n@@DONE@@\\n")
    sys.stderr.flush()
"""
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(
        sep_mod, "_spawn_worker_cmd", lambda device: [sys.executable, "-c", echo_worker]
    )

    sep_mod.separate(Job(id="abcdefabc277"), tmp_path / "source.wav", tmp_path)

    assert (tmp_path / "shifts.txt").read_text() == "1"


def test_dispatch_includes_shifts_2_at_best_quality(monkeypatch, tmp_path):
    echo_worker = """
import sys, json, os
for line in sys.stdin:
    req = json.loads(line)
    d = os.path.join(req["job_dir"], "htdemucs_6s", "source")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "vocals.wav"), "wb").write(b"RIFF")
    open(os.path.join(req["job_dir"], "shifts.txt"), "w").write(str(req["shifts"]))
    sys.stderr.write("100%\\n@@DONE@@\\n")
    sys.stderr.flush()
"""
    monkeypatch.setattr(sep_mod, "get_separation_quality", lambda: "best")
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(
        sep_mod, "_spawn_worker_cmd", lambda device: [sys.executable, "-c", echo_worker]
    )

    sep_mod.separate(Job(id="abcdefabc278"), tmp_path / "source.wav", tmp_path)

    assert (tmp_path / "shifts.txt").read_text() == "2"


def test_records_startup_timing_on_first_progress_line(job, tmp_path, monkeypatch):
    """#288/#309: time from dispatch to the first progress line demucs
    emits -- the full spawn/model-load cost for a fresh worker, near-zero
    for a reused warm one."""
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", _stub_spawns(set(), calls))

    sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert job.stage_timings is not None
    assert "separate_startup" in job.stage_timings
    assert job.stage_timings["separate_startup"] >= 0.0


def test_gpu_success_needs_no_fallback(job, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", _stub_spawns(set(), calls))

    stems_root = sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert calls == ["cuda"]
    assert stems_root.is_dir()
    assert job.gpu_fallback is False
    assert job.compute_device == "cuda"


def test_cpu_failure_does_not_retry(job, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", _stub_spawns({"cpu"}, calls))

    with pytest.raises(SeparationError) as exc_info:
        sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert calls == ["cpu"]  # exactly one attempt
    assert job.gpu_fallback is False
    assert exc_info.value.device == "cpu"


def test_both_attempts_failing_raises_with_both_tails(job, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "mps")
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", _stub_spawns({"mps", "cpu"}, calls))

    with pytest.raises(SeparationError) as exc_info:
        sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert calls == ["mps", "cpu"]
    err = exc_info.value
    assert err.device == "mps, then cpu"
    # The quarantine's error.txt gets both attempts' evidence.
    joined = "\n".join(err.tail)
    assert "--- attempt on mps ---" in joined
    assert "--- cpu fallback attempt ---" in joined


def test_cancel_during_gpu_attempt_skips_fallback(job, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", _stub_spawns({"cuda"}, calls))
    job.cancel_requested = True  # POST /cancel arrived before/mid attempt

    with pytest.raises(JobCancelled):
        sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert calls == ["cuda"]  # no CPU retry after a cancel


def test_partial_gpu_output_cleared_before_retry(job, tmp_path, monkeypatch):
    """A failed GPU attempt's partial stems must not leak into the CPU run."""
    calls: list[str] = []
    marker = tmp_path / sep_mod.DEMUCS_MODEL / "partial-garbage.wav"
    marker_repr = str(marker).replace("\\", "\\\\")

    def fake_spawn(device: str) -> list[str]:
        calls.append(device)
        if device == "cuda":
            # Simulate the worker dying after writing partial output.
            code = (
                "import os, sys, json\n"
                "sys.stdin.readline()\n"
                f"os.makedirs(os.path.dirname('{marker_repr}'), exist_ok=True)\n"
                f"open('{marker_repr}', 'wb').write(b'junk')\n"
                "sys.stderr.write('@@ERROR@@' + json.dumps('CUDA error') + chr(10))\n"
                "sys.stderr.flush()\n"
                "sys.exit(1)\n"
            )
        else:
            code = _SUCCESS_WORKER
        return [sys.executable, "-c", code]

    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", fake_spawn)

    sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert calls == ["cuda", "cpu"]
    assert not marker.exists(), "partial GPU output must be cleared before the CPU retry"


# ─── #309: worker reuse ────────────────────────────────────────────────────


def test_worker_reused_across_consecutive_jobs_on_same_device(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", _stub_spawns(set(), calls))

    for i in range(3):
        job = Job(id=f"abcdefabc30{i}")
        (tmp_path / "source.wav").write_bytes(b"RIFF")
        sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert calls == ["cpu"]  # one spawn serving all three jobs


def test_worker_respawned_on_device_change(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", _stub_spawns(set(), calls))

    devices = iter(["cpu", "cuda"])
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: next(devices))

    sep_mod.separate(Job(id="abcdefabc310"), tmp_path / "source.wav", tmp_path)
    sep_mod.separate(Job(id="abcdefabc311"), tmp_path / "source.wav", tmp_path)

    assert calls == ["cpu", "cuda"]


def test_worker_not_reused_after_failure(tmp_path, monkeypatch):
    """A failed job's worker is never handed the next job -- GPU/CUDA state
    afterward isn't something we can vouch for (see demucs_worker.py)."""
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", _stub_spawns({"cpu"}, calls))

    with pytest.raises(SeparationError):
        sep_mod.separate(Job(id="abcdefabc320"), tmp_path / "source.wav", tmp_path)
    # The next job on the same device spawns a fresh worker rather than
    # reusing the one that just failed.
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", _stub_spawns(set(), calls))
    sep_mod.separate(Job(id="abcdefabc321"), tmp_path / "source.wav", tmp_path)

    assert calls == ["cpu", "cpu"]  # two spawns: the failure, then the retry


def test_cancel_kills_worker_next_job_spawns_fresh(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", _stub_spawns({"cpu"}, calls))
    cancelled_job = Job(id="abcdefabc330")
    cancelled_job.cancel_requested = True

    with pytest.raises(JobCancelled):
        sep_mod.separate(cancelled_job, tmp_path / "source.wav", tmp_path)

    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", _stub_spawns(set(), calls))
    sep_mod.separate(Job(id="abcdefabc331"), tmp_path / "source.wav", tmp_path)

    assert calls == ["cpu", "cpu"]  # two spawns: cancelled, then fresh


# ─── #125: the model download is its own, bounded phase ───────────────────


def _separate_within(seconds: float, job, source: Path, job_dir: Path) -> Path:
    """separate(), failing the test if it is still running after `seconds`.
    The fixture's teardown kills the worker, which ends a run left waiting."""
    result: dict = {}

    def run():
        try:
            result["stems"] = sep_mod.separate(job, source, job_dir)
        except BaseException as exc:  # handed to the test thread below
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"the Split was still waiting after {seconds} s"
    if "error" in result:
        raise result["error"]
    return result["stems"]


def _record_stages(monkeypatch) -> list[str]:
    stages: list[str] = []
    real = sep_mod._set

    def record(job, **fields):
        if "stage" in fields:
            stages.append(fields["stage"])
        real(job, **fields)

    monkeypatch.setattr(sep_mod, "_set", record)
    return stages


def test_a_stalled_model_host_fails_the_split_in_seconds_and_names_the_file(
    job, tmp_path, monkeypatch
):
    """The real worker, real demucs and torch.hub's real download, pointed at
    a local host that accepts the connection and never answers. The old
    worker waited on it for the 30-minute stall limit, then again on CPU."""
    calls: list[str] = []
    with socket.create_server(("127.0.0.1", 0)) as server:
        root_url = f"http://127.0.0.1:{server.getsockname()[1]}/demucs/"
        accepted: list[socket.socket] = []

        def stall():
            with contextlib.suppress(OSError):
                while True:
                    accepted.append(server.accept()[0])

        threading.Thread(target=stall, daemon=True).start()

        def spawn(device: str) -> list[str]:
            calls.append(device)
            code = (
                "import sys, demucs.pretrained as p; p.ROOT_URL = sys.argv[2]; "
                "from app.pipeline.demucs_worker import main; main()"
            )
            return [sys.executable, "-c", code, device, root_url]

        monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", spawn)
        monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
        monkeypatch.setenv("TORCH_HOME", str(tmp_path / "torch"))
        monkeypatch.setenv("STEMDECK_TIMEOUT_MODEL_DOWNLOAD_READ", "1")
        monkeypatch.setenv("STEMDECK_MODEL_DOWNLOAD_ATTEMPTS", "2")
        try:
            with pytest.raises(SeparationError) as exc_info:
                _separate_within(60, job, tmp_path / "source.wav", tmp_path)
        finally:
            for conn in accepted:
                conn.close()

    url = f"{root_url}hybrid_transformer/5c90dfd2-34c22ccb.th"
    folder = tmp_path / "torch" / "hub" / "checkpoints"
    assert str(exc_info.value) == (
        f"demucs failed: model download failed: {url}; put the file in {folder} (no data for 1 s)"
    )
    assert classify_failure("\n".join(exc_info.value.tail)) == "model-download"
    # The CPU worker would fetch the same file from the same host.
    assert calls == ["cuda"]
    assert job.gpu_fallback is False
    assert not list(folder.iterdir()), "a failed download left a file behind"


# Says where it is downloading, shows a progress bar, then the host goes quiet.
_STALLED_DOWNLOAD_WORKER = """
import sys
sys.stderr.write('Downloading: "http://models.example/demucs/5c90dfd2-34c22ccb.th" to '
                 '/cache/torch/hub/checkpoints/5c90dfd2-34c22ccb.th\\n')
sys.stderr.write("  0%|          | 0.00/80.2M [00:00<?, ?B/s]\\r")
sys.stderr.flush()
sys.stdin.read()
"""


def test_a_worker_silent_before_ready_is_stopped_at_the_short_limit(job, tmp_path, monkeypatch):
    calls: list[str] = []
    stages = _record_stages(monkeypatch)

    def spawn(device: str) -> list[str]:
        calls.append(device)
        return [sys.executable, "-c", _STALLED_DOWNLOAD_WORKER]

    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", spawn)
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    monkeypatch.setattr(sep_mod, "TIMEOUT_MODEL_STALL", 1)

    with pytest.raises(SeparationError) as exc_info:
        _separate_within(20, job, tmp_path / "source.wav", tmp_path)

    assert exc_info.value.tail[-1] == (
        "model download failed: http://models.example/demucs/5c90dfd2-34c22ccb.th"
        "; put the file in /cache/torch/hub/checkpoints (no data for 1 s)"
    )
    assert calls == ["cuda"]
    assert "Downloading the separation model 0%" in stages
    assert not any(s.startswith("Separating 0") for s in stages)


def test_a_slow_start_with_a_cached_model_is_not_held_to_the_short_limit(
    job, tmp_path, monkeypatch
):
    """Importing torch on a cold machine and loading a model already on disk
    say nothing until READY. That is not a download, so the short limit,
    which only a download line switches on, must not stop it."""
    slow_start = """
import sys, json, os, time
time.sleep(3)  # importing torch, loading the cached model
sys.stderr.write("@@READY@@\\n")
sys.stderr.flush()
for line in sys.stdin:
    req = json.loads(line)
    d = os.path.join(req["job_dir"], "htdemucs_6s", "source")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "vocals.wav"), "wb").write(b"RIFF")
    sys.stderr.write("@@DONE@@\\n")
    sys.stderr.flush()
"""
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", slow_start])
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep_mod, "TIMEOUT_MODEL_STALL", 1)

    stems_root = _separate_within(30, job, tmp_path / "source.wav", tmp_path)

    assert (stems_root / "vocals.wav").is_file()


# Downloads its model on start, then serves jobs; a reused worker does not.
_DOWNLOADING_WORKER = """
import sys, json, os
sys.stderr.write('Downloading: "http://models.example/m-0123abcd.th" to /c/m-0123abcd.th\\n')
sys.stderr.write(" 45%|####      | 36.1M/80.2M\\r100%|##########| 80.2M/80.2M\\n@@READY@@\\n")
sys.stderr.flush()
for line in sys.stdin:
    req = json.loads(line)
    d = os.path.join(req["job_dir"], "htdemucs_6s", "source")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "vocals.wav"), "wb").write(b"RIFF")
    sys.stderr.write(" 30%|###       |\\r@@DONE@@\\n")
    sys.stderr.flush()
"""


def test_progress_before_ready_is_the_download_and_after_it_separation(tmp_path, monkeypatch):
    stages = _record_stages(monkeypatch)
    monkeypatch.setattr(
        sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", _DOWNLOADING_WORKER]
    )
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    first = Job(id="abcdefabc340")

    sep_mod.separate(first, tmp_path / "source.wav", tmp_path)

    assert [s for s in stages if s.endswith("%")] == [
        "Downloading the separation model 45%",
        "Downloading the separation model 100%",
        "Separating 30%",
    ]
    assert first.progress == pytest.approx(0.30)
    assert "separate_startup" in (first.stage_timings or {})

    # The warm worker said READY on the first job: its progress is separation.
    stages.clear()
    sep_mod.separate(Job(id="abcdefabc341"), tmp_path / "source.wav", tmp_path)
    assert [s for s in stages if s.endswith("%")] == ["Separating 30%"]


# The mirror's copy arrives whole but wrong, and the original host's follows:
# the lines app/core/model_download.py writes between the two.
_MIRROR_FALLBACK_WORKER = """
import sys, json, os
sys.stderr.write('Downloading: "http://models.example/m-0123abcd.th" to /c/m-0123abcd.th\\n')
sys.stderr.write("100%|##########| 80.2M/80.2M\\n")
sys.stderr.write("model mirror failed (SHA-256 does not match), downloading from the original host\\n")
sys.stderr.write('Downloading: "http://models.example/m-0123abcd.th" to /c/m-0123abcd.th\\n')
sys.stderr.write(" 60%|######    |\\r100%|##########| 80.2M/80.2M\\n@@READY@@\\n")
sys.stderr.flush()
for line in sys.stdin:
    req = json.loads(line)
    d = os.path.join(req["job_dir"], "htdemucs_6s", "source")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "vocals.wav"), "wb").write(b"RIFF")
    sys.stderr.write(" 30%|###       |\\r@@DONE@@\\n")
    sys.stderr.flush()
"""


def test_the_download_after_a_failed_mirror_still_reads_as_the_download(tmp_path, monkeypatch):
    stages = _record_stages(monkeypatch)
    monkeypatch.setattr(
        sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", _MIRROR_FALLBACK_WORKER]
    )
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    job = Job(id="abcdefabc342")

    sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert [s for s in stages if s.endswith("%")] == [
        "Downloading the separation model 100%",
        "Downloading the separation model 60%",
        "Downloading the separation model 100%",
        "Separating 30%",
    ]
    assert job.progress == pytest.approx(0.30)


def test_after_ready_a_silent_worker_gets_the_long_limit(job, tmp_path, monkeypatch):
    """Inference on a GPU can be silent for minutes; only the model's load and
    download are held to the short limit."""
    quiet = """
import sys, json, os, time
sys.stderr.write("@@READY@@\\n")
sys.stderr.flush()
req = json.loads(sys.stdin.readline())
time.sleep(3)  # separating, without a word
d = os.path.join(req["job_dir"], "htdemucs_6s", "source")
os.makedirs(d, exist_ok=True)
open(os.path.join(d, "vocals.wav"), "wb").write(b"RIFF")
sys.stderr.write("@@DONE@@\\n")
sys.stderr.flush()
sys.stdin.read()
"""
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", quiet])
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep_mod, "TIMEOUT_MODEL_STALL", 1)

    stems_root = _separate_within(30, job, tmp_path / "source.wav", tmp_path)

    assert (stems_root / "vocals.wav").is_file()


def test_a_model_download_failure_is_not_retried_on_cpu(job, tmp_path, monkeypatch):
    attempts: list[str] = []

    def fake_run(job, source, job_dir, device):
        attempts.append(device)
        return 1, ["model download failed: http://h/m.th; put the file in /c (HTTP 403)"]

    monkeypatch.setattr(sep_mod, "_run_demucs", fake_run)
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")

    with pytest.raises(SeparationError) as exc_info:
        sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert attempts == ["cuda"]
    assert exc_info.value.device == "cuda"
    assert job.gpu_fallback is False
