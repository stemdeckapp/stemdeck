"""The idle unload of the separation worker and the beat model (#975).

The worker is a real process here, the stub _spawn_worker_cmd seam other
separation tests use, so "unloaded" means the process is gone and "reloaded"
means a new one served the next job. Only time is fake: each resource reads a
clock the test moves.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

from app.core import settings
from app.core.models import Job, JobCancelled
from app.pipeline import beat_detect as bd
from app.pipeline import idle_unload, jobqueue, runner
from app.pipeline import separate as sep_mod
from app.pipeline.idle_unload import IdleResource

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

# Reports progress, then waits for a line on stdin before finishing, so a test
# can hold a separation open while it tries to unload the worker under it.
_HELD_WORKER = """
import sys, json, os
sys.stderr.write("@@READY@@\\n")
req = json.loads(sys.stdin.readline())
sys.stderr.write("5%\\n")
sys.stderr.flush()
sys.stdin.readline()
d = os.path.join(req["job_dir"], "htdemucs_6s", "source")
os.makedirs(d, exist_ok=True)
open(os.path.join(d, "vocals.wav"), "wb").write(b"RIFF")
sys.stderr.write("100%\\n@@DONE@@\\n")
sys.stderr.flush()
"""

MIN = 60.0


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def clock(monkeypatch) -> Clock:
    c = Clock()
    for res in (sep_mod.WORKER, bd.MODEL):
        monkeypatch.setattr(res, "_clock", c)
        monkeypatch.setattr(res, "_last_used", c.t)
        assert res._users == 0
    return c


@pytest.fixture(autouse=True)
def _reset():
    sep_mod._worker.clear()
    sep_mod._left_behind.clear()
    bd.reset_model_cache()
    yield
    sep_mod._kill_worker()
    sep_mod._left_behind.clear()
    bd.reset_model_cache()


@pytest.fixture()
def spawns(monkeypatch) -> list[str]:
    calls: list[str] = []

    def spawn(device: str) -> list[str]:
        calls.append(device)
        return [sys.executable, "-c", _SUCCESS_WORKER]

    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", spawn)
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    return calls


def _separate(tmp_path: Path, name: str) -> Job:
    job = Job(id=f"{name:0<12}"[:12])
    job_dir = tmp_path / name
    job_dir.mkdir()
    (job_dir / "source.wav").write_bytes(b"RIFF")
    stems = sep_mod.separate(job, job_dir / "source.wav", job_dir)
    assert (stems / "vocals.wav").is_file()
    return job


# --- the worker ---------------------------------------------------------------


async def test_worker_unloads_after_the_idle_minutes_and_the_next_job_reloads_it(
    tmp_path, clock, spawns, monkeypatch
):
    monkeypatch.setattr(settings, "get_idle_unload_minutes", lambda: 10)
    monkeypatch.setattr(idle_unload, "get_idle_unload_minutes", lambda: 10)
    _separate(tmp_path, "aaaa")
    first = sep_mod._worker["proc"]
    assert first.poll() is None, "warm after a success, as before"

    clock.t += 9 * MIN
    assert await idle_unload.check() == []
    assert first.poll() is None, "not before the idle minutes are up"

    clock.t += 1 * MIN
    assert "separation worker" in await idle_unload.check()
    assert first.poll() is not None, "the process is gone, not just forgotten"
    assert "proc" not in sep_mod._worker

    _separate(tmp_path, "bbbb")
    assert spawns == ["cpu", "cpu"], "the next job started a new worker"
    assert sep_mod._worker["proc"] is not first
    assert sep_mod._worker["proc"].poll() is None


async def test_idle_counts_from_the_end_of_the_last_job(tmp_path, clock, spawns, monkeypatch):
    monkeypatch.setattr(idle_unload, "get_idle_unload_minutes", lambda: 10)
    clock.t += 60 * MIN  # long before this job: does not count as idle time
    _separate(tmp_path, "aaaa")
    clock.t += 5 * MIN
    assert await idle_unload.check() == []
    assert sep_mod._worker["proc"].poll() is None


async def test_zero_never_unloads(tmp_path, clock, spawns, monkeypatch):
    monkeypatch.setattr(idle_unload, "get_idle_unload_minutes", lambda: 0)
    _separate(tmp_path, "aaaa")
    clock.t += 7 * 24 * 60 * MIN
    assert await idle_unload.check() == []
    assert sep_mod._worker["proc"].poll() is None


def test_not_while_a_separation_runs(tmp_path, clock, monkeypatch):
    """The worker is in use for the whole separation: an unload that comes due
    in the middle of it waits for the job, whatever the clock says."""
    monkeypatch.setattr(
        sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", _HELD_WORKER]
    )
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    job = Job(id="cccccccccccc")
    (tmp_path / "source.wav").write_bytes(b"RIFF")
    done: list[object] = []
    t = threading.Thread(
        target=lambda: done.append(sep_mod.separate(job, tmp_path / "source.wav", tmp_path))
    )
    t.start()
    try:
        for _ in range(200):
            if job.progress > 0:
                break
            threading.Event().wait(0.05)
        assert job.progress > 0, "the stub worker started separating"
        clock.t += 24 * 60 * MIN
        assert sep_mod.WORKER.unload_if_idle(10 * MIN) is False
        proc = sep_mod._worker["proc"]
        assert proc.poll() is None
    finally:
        proc = sep_mod._worker.get("proc")
        if proc is not None and proc.stdin is not None:
            proc.stdin.write("go\n")
            proc.stdin.flush()
        t.join(10)
    assert done and Path(done[0]).is_dir(), "the job finished on the same worker"
    # Idle from the end of that job, so not yet.
    assert sep_mod.WORKER.unload_if_idle(10 * MIN) is False
    clock.t += 10 * MIN
    assert sep_mod.WORKER.unload_if_idle(10 * MIN) is True


@pytest.mark.parametrize("busy", ["running", "queued", "pipeline_lock", "stopping"])
async def test_the_timer_does_not_look_while_the_pipeline_is_busy(
    tmp_path, clock, spawns, monkeypatch, busy
):
    monkeypatch.setattr(idle_unload, "get_idle_unload_minutes", lambda: 10)
    _separate(tmp_path, "aaaa")
    clock.t += 60 * MIN
    if busy == "running":
        jobqueue._set_running("dddddddddddd")
    elif busy == "queued":
        jobqueue._queue.append("dddddddddddd")
    elif busy == "stopping":
        monkeypatch.setattr(jobqueue, "_stopping", True)
    try:
        if busy == "pipeline_lock":
            async with runner._pipeline_lock:
                assert await idle_unload.check() == []
        else:
            assert await idle_unload.check() == []
    finally:
        jobqueue._set_running(None)
        jobqueue._queue.clear()
        monkeypatch.setattr(jobqueue, "_stopping", False)
    assert sep_mod._worker["proc"].poll() is None
    # The busy look counted as a use: idle starts again from there.
    assert await idle_unload.check() == []
    clock.t += 10 * MIN
    assert "separation worker" in await idle_unload.check()


async def test_idle_counts_from_when_the_pipeline_goes_quiet(tmp_path, clock, spawns, monkeypatch):
    """Lyrics and sections run after the separation, inside the same job. A
    12 minute stage after it must not count as 12 idle minutes."""
    monkeypatch.setattr(idle_unload, "get_idle_unload_minutes", lambda: 10)
    _separate(tmp_path, "aaaa")
    jobqueue._set_running("aaaa00000000")  # the job's later stages
    try:
        for _ in range(24):  # 12 minutes of 30 s ticks
            clock.t += 30
            assert await idle_unload.check() == []
    finally:
        jobqueue._set_running(None)
    clock.t += 9 * MIN
    assert await idle_unload.check() == [], "only 9 quiet minutes"
    assert sep_mod._worker["proc"].poll() is None
    clock.t += 1 * MIN
    assert "separation worker" in await idle_unload.check()


async def test_a_paused_queue_with_waiting_jobs_counts_as_idle(
    tmp_path, clock, spawns, monkeypatch
):
    """Restored jobs wait for Start, possibly for days; nothing runs until
    then, so they do not keep the worker loaded."""
    monkeypatch.setattr(idle_unload, "get_idle_unload_minutes", lambda: 10)
    _separate(tmp_path, "aaaa")
    clock.t += 10 * MIN
    monkeypatch.setattr(jobqueue, "_paused", True)
    jobqueue._queue.append("dddddddddddd")
    try:
        assert "separation worker" in await idle_unload.check()
    finally:
        jobqueue._queue.clear()


def test_the_after_unload_hook_runs_outside_the_lock():
    clock = Clock()
    seen: list[bool] = []
    res = IdleResource(
        "x",
        lambda: True,
        lambda: None,
        clock=clock,
        after_unload=lambda: seen.append(res._lock.locked()),
    )
    clock.t += 2 * MIN
    assert res.unload_if_idle(MIN) is True
    assert seen == [False]


def test_a_job_that_arrives_during_an_unload_waits_and_then_loads_afresh(
    tmp_path, clock, spawns, monkeypatch
):
    _separate(tmp_path, "aaaa")
    first = sep_mod._worker["proc"]
    clock.t += 60 * MIN

    entered = threading.Event()
    release = threading.Event()
    real_kill = sep_mod._kill_worker

    def slow_kill() -> None:
        entered.set()
        release.wait(10)
        real_kill()

    monkeypatch.setattr(sep_mod, "_kill_worker", slow_kill)
    unloader = threading.Thread(target=sep_mod.WORKER.unload_if_idle, args=(10 * MIN,))
    unloader.start()
    assert entered.wait(5)

    result: list[object] = []
    job = threading.Thread(target=lambda: result.append(_separate(tmp_path, "bbbb")))
    job.start()
    job.join(0.5)
    assert job.is_alive(), "the job waits for the unload rather than racing it"
    monkeypatch.setattr(sep_mod, "_kill_worker", real_kill)
    release.set()
    unloader.join(10)
    job.join(10)
    assert result, "and then it finished"
    assert first.poll() is not None
    assert spawns == ["cpu", "cpu"]
    assert sep_mod._worker["proc"].poll() is None


def test_a_cancel_during_an_unload_leaves_no_worker(tmp_path, clock, spawns, monkeypatch):
    """Cancelled while it waited for the unload: the worker it then starts is
    stopped the moment it is registered (registry.set_proc, #748), the job ends
    cancelled, and nothing is left running or counted as in use."""
    from app.core import registry

    monkeypatch.setattr(registry, "_jobs", {})
    _separate(tmp_path, "aaaa")
    clock.t += 60 * MIN
    entered = threading.Event()
    release = threading.Event()
    real_kill = sep_mod._kill_worker

    def slow_kill() -> None:
        entered.set()
        release.wait(10)
        real_kill()

    monkeypatch.setattr(sep_mod, "_kill_worker", slow_kill)
    unloader = threading.Thread(target=sep_mod.WORKER.unload_if_idle, args=(10 * MIN,))
    unloader.start()
    assert entered.wait(5)

    job = registry.register(Job(id="eeeeeeeeeeee", status="separating"))
    (tmp_path / "source.wav").write_bytes(b"RIFF")
    raised: list[BaseException] = []

    def run() -> None:
        try:
            sep_mod.separate(job, tmp_path / "source.wav", tmp_path)
        except BaseException as e:
            raised.append(e)

    t = threading.Thread(target=run)
    t.start()
    job.cancel_requested = True
    monkeypatch.setattr(sep_mod, "_kill_worker", real_kill)
    release.set()
    unloader.join(10)
    t.join(10)
    assert raised and isinstance(raised[0], JobCancelled)
    assert not (tmp_path / "htdemucs_6s").exists(), "the cancelled job separated nothing"
    assert sep_mod.WORKER._users == 0, "the use was released"
    assert "proc" not in sep_mod._worker, "no worker is left running"


class _ClosedStdin:
    """A worker already stopped when the request is written: what Linux gives
    when set_proc() has terminated it first (#748). Windows buffers the write,
    so without this the branch is reached only on Linux."""

    def __init__(self) -> None:
        import io

        self.stdin = self
        self.stderr = io.StringIO("")
        self.pid = 1

    def write(self, _data: str) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        pass

    def poll(self):
        return 0


@pytest.mark.parametrize("cancelled", [True, False])
def test_a_worker_gone_before_dispatch_is_a_cancel_only_when_the_job_was_cancelled(
    tmp_path, monkeypatch, cancelled
):
    from app.pipeline.errors import SeparationError

    monkeypatch.setattr(sep_mod, "_get_worker", lambda device: _ClosedStdin())
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    job = Job(id="jjjjjjjjjjjj")
    job.cancel_requested = cancelled
    (tmp_path / "source.wav").write_bytes(b"RIFF")
    expected = JobCancelled if cancelled else SeparationError
    with pytest.raises(expected):
        sep_mod.separate(job, tmp_path / "source.wav", tmp_path)
    assert sep_mod.WORKER._users == 0


def test_shutdown_waits_for_an_unload_in_progress(tmp_path, clock, spawns, monkeypatch):
    _separate(tmp_path, "aaaa")
    clock.t += 60 * MIN
    entered = threading.Event()
    release = threading.Event()
    kills: list[str] = []
    real_kill = sep_mod._kill_worker

    def slow_kill() -> None:
        kills.append("idle")
        entered.set()
        release.wait(10)
        real_kill()

    monkeypatch.setattr(sep_mod, "_kill_worker", slow_kill)
    unloader = threading.Thread(target=sep_mod.WORKER.unload_if_idle, args=(10 * MIN,))
    unloader.start()
    assert entered.wait(5)

    def shutdown_kill() -> None:
        kills.append("shutdown")
        real_kill()

    shutdown = threading.Thread(target=sep_mod.shutdown_worker)
    monkeypatch.setattr(sep_mod, "_kill_worker", shutdown_kill)
    shutdown.start()
    shutdown.join(0.5)
    assert shutdown.is_alive(), "shutdown waits for the unload holding the worker"
    release.set()
    unloader.join(10)
    shutdown.join(10)
    assert kills == ["idle", "shutdown"]
    assert "proc" not in sep_mod._worker


def test_shutdown_stops_a_worker_under_a_running_job(tmp_path, clock, monkeypatch):
    """Unlike the idle unload, shutdown does not wait for the job: the runner
    reads the stopped worker as an interruption (test_separate_shutdown)."""
    monkeypatch.setattr(
        sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", _HELD_WORKER]
    )
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    job = Job(id="ffffffffffff")
    (tmp_path / "source.wav").write_bytes(b"RIFF")
    out: list[object] = []

    def run() -> None:
        try:
            out.append(sep_mod.separate(job, tmp_path / "source.wav", tmp_path))
        except BaseException as e:
            out.append(e)

    t = threading.Thread(target=run)
    t.start()
    for _ in range(200):
        if job.progress > 0:
            break
        threading.Event().wait(0.05)
    proc = sep_mod._worker["proc"]
    sep_mod.shutdown_worker()
    t.join(10)
    assert not t.is_alive()
    assert proc.poll() is not None
    assert sep_mod.WORKER._users == 0


async def test_lifespan_cancels_the_timer_and_stops_the_worker(
    tmp_path, clock, spawns, monkeypatch
):
    import app.main as main_mod

    async def no_sweep():
        pass

    monkeypatch.setattr(main_mod, "_sweep_loop", no_sweep)
    monkeypatch.delenv("STEMDECK_DESKTOP", raising=False)
    monkeypatch.setattr(main_mod, "HTTPS_PORT", None)
    timer: list[str] = []

    async def fake_run() -> None:
        timer.append("started")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            timer.append("cancelled")
            raise

    monkeypatch.setattr(idle_unload, "run", fake_run)
    _separate(tmp_path, "aaaa")
    proc = sep_mod._worker["proc"]
    async with main_mod.lifespan(main_mod.app):
        await asyncio.sleep(0)
        assert timer == ["started"]
    await asyncio.sleep(0)
    assert timer == ["started", "cancelled"]
    assert proc.poll() is not None, "no worker outlives the server"


async def test_the_timer_survives_a_failing_unload(monkeypatch, caplog):
    calls: list[str] = []

    def boom() -> None:
        calls.append("boom")
        raise OSError("access denied")

    clock = Clock()
    bad = IdleResource("bad", lambda: True, boom, clock=clock)
    good = IdleResource("good", lambda: True, lambda: calls.append("good"), clock=clock)
    monkeypatch.setattr(idle_unload, "_resources", [bad, good])
    monkeypatch.setattr(idle_unload, "get_idle_unload_minutes", lambda: 1)
    clock.t += 2 * MIN
    assert await idle_unload.check() == ["good"]
    assert calls == ["boom", "good"]
    assert "could not unload bad" in caplog.text


async def test_run_checks_on_its_interval(monkeypatch):
    seen: list[int] = []

    async def fake_check() -> list[str]:
        seen.append(1)
        if len(seen) == 1:
            raise RuntimeError("one bad check does not end the timer")
        return []

    monkeypatch.setattr(idle_unload, "check", fake_check)
    task = asyncio.create_task(idle_unload.run(interval_sec=0.01))
    for _ in range(100):
        if len(seen) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(seen) >= 3


# --- the beat model -----------------------------------------------------------


class _FakeBeats:
    def __call__(self, y, sr):
        return [0.5, 1.0, 1.5], [0.5]


async def test_beat_model_drops_when_idle_and_the_next_detection_reloads_it(clock, monkeypatch):
    loads: list[int] = []

    def fake_get_model():
        if bd._model is None:
            loads.append(1)
            bd._model = _FakeBeats()
        return bd._model

    monkeypatch.setattr(bd, "_get_model", fake_get_model)
    monkeypatch.setattr(idle_unload, "get_idle_unload_minutes", lambda: 10)
    assert bd._detect_model(None, 22050) is not None
    assert bd._model is not None

    clock.t += 10 * MIN
    assert "beat model" in await idle_unload.check()
    assert bd._model is None

    assert bd._detect_model(None, 22050) == ([0.5, 1.0, 1.5], [0.5])
    assert loads == [1, 1], "loaded again on the next use"


def test_beat_model_is_not_dropped_during_a_detection(clock, monkeypatch):
    seen: list[bool] = []

    class Slow:
        def __call__(self, y, sr):
            clock.t += 60 * MIN
            seen.append(bd.MODEL.unload_if_idle(10 * MIN))
            return [0.5, 1.0], []

    monkeypatch.setattr(bd, "_get_model", lambda: Slow())
    monkeypatch.setattr(bd, "_model", Slow())
    assert bd._detect_model(None, 22050) is not None
    assert seen == [False]


def test_a_cached_beat_model_failure_is_not_undone_by_the_timer(clock):
    bd._model_failed = True
    clock.t += 60 * MIN
    assert bd.MODEL.unload_if_idle(10 * MIN) is False
    assert bd._model_failed is True


# --- the setting --------------------------------------------------------------


def test_setting_defaults_to_ten_and_clamps(monkeypatch):
    assert settings.get_idle_unload_minutes() == 10
    assert settings.set_idle_unload_minutes(0) == 0
    assert settings.get_idle_unload_minutes() == 0
    assert settings.set_idle_unload_minutes(-5) == 0
    assert settings.set_idle_unload_minutes(10**6) == 1440
    assert settings.set_idle_unload_minutes(30) == 30


def test_setting_persists(monkeypatch):
    import json

    settings.set_idle_unload_minutes(25)
    on_disk = json.loads(settings._SETTINGS_PATH.read_text(encoding="utf-8"))
    assert on_disk["idle_unload_minutes"] == 25
    monkeypatch.setattr(settings, "_state", None)
    assert settings.get_idle_unload_minutes() == 25


def test_environment_seeds_the_default_and_a_saved_value_wins(monkeypatch):
    monkeypatch.setattr(settings, "IDLE_UNLOAD_MINUTES", 45)
    assert settings.get_idle_unload_minutes() == 45
    settings.set_idle_unload_minutes(5)
    assert settings.get_idle_unload_minutes() == 5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 10), ("0", 0), ("30", 30), ("-3", 0), ("99999", 1440), ("ten", 10)],
)
def test_environment_variable_is_parsed_and_clamped(monkeypatch, raw, expected):
    from app.core import config

    monkeypatch.setenv("STEMDECK_IDLE_UNLOAD_MINUTES", raw)
    assert config._env_idle_unload_minutes() == expected


def test_a_stored_garbage_value_falls_back_to_the_default(monkeypatch):
    settings._ensure()["idle_unload_minutes"] = "soon"
    assert settings.get_idle_unload_minutes() == 10
    settings._ensure()["idle_unload_minutes"] = True
    assert settings.get_idle_unload_minutes() == 10


def test_settings_api_reads_and_writes_it():
    from fastapi.testclient import TestClient

    import app.main as main_mod

    with TestClient(main_mod.app) as c:
        got = c.get("/api/settings").json()
        assert got["idle_unload_minutes"] == 10
        assert got["idle_unload_minutes_max"] == 1440

        r = c.post("/api/settings", json={"idle_unload_minutes": 3})
        assert r.status_code == 200 and r.json()["idle_unload_minutes"] == 3
        assert c.get("/api/settings").json()["idle_unload_minutes"] == 3

        r = c.post("/api/settings", json={"idle_unload_minutes": 0})
        assert r.status_code == 200 and r.json()["idle_unload_minutes"] == 0

        r = c.post("/api/settings", json={"idle_unload_minutes": 50000})
        assert r.json()["idle_unload_minutes"] == 1440

        r = c.post("/api/settings", json={"idle_unload_minutes": "soon"})
        assert r.status_code == 422
        r = c.post("/api/settings", json={"idle_unload_minutes": None})
        assert r.status_code == 422
        assert c.get("/api/settings").json()["idle_unload_minutes"] == 1440, "nothing changed"


# --- stopping the worker --------------------------------------------------------


def test_a_stopped_worker_leaves_no_open_pipe(spawns):
    """Its pipes close with it, not whenever the Popen is collected: under
    -X dev that was a ResourceWarning for each stopped worker."""
    import gc
    import warnings

    proc = sep_mod._get_worker("cpu")
    pipes = (proc.stdin, proc.stderr)
    sep_mod._kill_worker()
    assert all(p.closed for p in pipes)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        del proc, pipes
        gc.collect()
    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []


class Wedged:
    """A worker that ignores terminate() and kill(), with real pipes."""

    def __init__(self, pid: int = 4242) -> None:
        import os

        self.pid = pid
        self.calls: list[str] = []
        self.exited = False
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        self.stdin = os.fdopen(w1, "w")
        self.stderr = os.fdopen(r2, "r")
        self._other_ends = (os.fdopen(r1, "r"), os.fdopen(w2, "w"))

    def poll(self):
        return 0 if self.exited else None

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")

    def wait(self, timeout=None):
        import subprocess

        self.calls.append(f"wait {timeout}")
        raise subprocess.TimeoutExpired("demucs", timeout)

    def communicate(self, *a, **k):
        raise AssertionError("an unbounded reap")

    def close_all(self) -> None:
        for f in (self.stdin, self.stderr, *self._other_ends):
            try:
                f.close()
            except OSError:
                pass


@pytest.fixture()
def wedged(monkeypatch):
    w = Wedged()
    monkeypatch.setattr(sep_mod, "_KILL_REAP_TIMEOUT_SEC", 0.01)
    yield w
    w.close_all()


def test_a_worker_that_will_not_die_is_given_up_on(wedged, caplog):
    """A worker wedged in an uninterruptible call must not hold the idle lock,
    and with it the next job and shutdown, forever."""
    sep_mod._worker.update(proc=wedged, device="cuda")
    sep_mod._kill_worker()
    assert wedged.calls == ["terminate", "wait 5", "kill", "wait 0.01"]
    assert "did not exit" in caplog.text
    assert "proc" not in sep_mod._worker
    # Left open: a job thread may still be blocked reading them.
    assert not wedged.stdin.closed and not wedged.stderr.closed
    assert [(e.proc, e.device) for e in sep_mod._left_behind] == [(wedged, "cuda")], "still tracked"


def test_no_second_gpu_worker_beside_a_left_behind_one(
    wedged, tmp_path, spawns, monkeypatch, caplog
):
    """It may still hold the card's memory: the job goes to the CPU, and says
    why, rather than failing on memory beside it."""
    sep_mod._worker.update(proc=wedged, device="cuda")
    sep_mod._kill_worker()
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    job = _separate(tmp_path, "gggg")
    assert spawns == ["cpu"], "no cuda worker was started"
    assert job.gpu_fallback is True
    assert "has not exited yet" in caplog.text
    assert "kill" in wedged.calls[4:], "killed once more"

    # Once it has exited, it is forgotten and the GPU is used again.
    wedged.exited = True
    _separate(tmp_path, "hhhh")
    assert spawns == ["cpu", "cuda"]
    assert sep_mod._left_behind == []
    assert wedged.stderr.closed, "its pipes close once it has exited"


def test_a_leftover_that_never_exits_stops_blocking_the_gpu_after_three_kills(
    wedged, tmp_path, spawns, monkeypatch, caplog
):
    """One that never exits must not turn GPU separation off for good: after
    three more kills the next GPU worker starts beside it, and memory, if it
    runs out, is the ordinary GPU-to-CPU retry's to handle."""
    sep_mod._worker.update(proc=wedged, device="cuda")
    sep_mod._kill_worker()
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    for name in ("j1", "j2", "j3"):
        assert _separate(tmp_path, name).gpu_fallback is True, "held back"
    assert spawns == ["cpu"], "no GPU worker for the first three"
    assert _separate(tmp_path, "j4").gpu_fallback is not True
    assert spawns[-1] == "cuda", "then the GPU is tried again"
    assert caplog.text.count("no longer holding") == 1, "said once"
    _separate(tmp_path, "j5")
    assert caplog.text.count("no longer holding") == 1
    assert [e.proc for e in sep_mod._left_behind] == [wedged], "still tracked for shutdown"


def test_a_leftover_stops_blocking_after_ten_minutes(wedged, tmp_path, spawns, monkeypatch):
    sep_mod._worker.update(proc=wedged, device="cuda")
    sep_mod._kill_worker()
    sep_mod._left_behind[0].since -= sep_mod._LEFT_BEHIND_MAX_SEC
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    _separate(tmp_path, "k1")
    assert spawns == ["cuda"]


def test_shutdown_tries_a_left_behind_worker_once_more(wedged, caplog):
    sep_mod._worker.update(proc=wedged, device="cuda")
    sep_mod._kill_worker()
    before = len(wedged.calls)
    sep_mod.shutdown_worker()
    assert wedged.calls[before:] == ["kill"]
    assert "still running at shutdown" in caplog.text


def test_a_pipe_closed_under_the_reader_reads_as_the_worker_exiting(tmp_path, monkeypatch):
    """A shutdown's _kill_worker closes stderr while the job thread reads it.
    That is EOF, a failed run, not a ValueError out of the pipeline."""
    import io

    from app.pipeline.errors import SeparationError

    monkeypatch.setattr(
        sep_mod, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", _HELD_WORKER]
    )
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    job = Job(id="iiiiiiiiiiii")
    (tmp_path / "source.wav").write_bytes(b"RIFF")
    out: list[BaseException] = []

    def run() -> None:
        try:
            sep_mod.separate(job, tmp_path / "source.wav", tmp_path)
        except BaseException as e:
            out.append(e)

    t = threading.Thread(target=run)
    t.start()
    for _ in range(200):
        if job.progress > 0:
            break
        threading.Event().wait(0.05)
    proc = sep_mod._worker["proc"]
    real = proc.stderr
    closed = io.StringIO()
    closed.close()
    # The next read after the current one meets a closed file.
    proc.stderr = closed
    proc.stdin.write("go" + chr(10))
    proc.stdin.flush()
    t.join(10)
    real.close()
    assert out and isinstance(out[0], SeparationError), out
    assert "proc" not in sep_mod._worker


def test_the_beat_model_collects_garbage_outside_the_idle_lock():
    import gc

    assert bd.MODEL._after_unload is gc.collect


# --- POST /api/settings refuses what would change the setting by accident -----


@pytest.mark.parametrize(
    "raw",
    [
        '{"idle_unload_minutes": true}',
        '{"idle_unload_minutes": false}',
        '{"idle_unload_minutes": "5"}',
        '{"idle_unload_minutes": "soon"}',
        '{"idle_unload_minutes": -1}',
        '{"idle_unload_minutes": 5.5}',
        '{"idle_unload_minutes": Infinity}',
        '{"idle_unload_minutes": 1e400}',
        '{"idle_unload_minutes": NaN}',
        '{"idle_unload_minutes": null}',
        '{"idle_unload_minutes": [5]}',
    ],
)
def test_settings_api_refuses_a_value_that_is_not_whole_minutes(raw):
    from fastapi.testclient import TestClient

    import app.main as main_mod

    settings.set_idle_unload_minutes(20)
    with TestClient(main_mod.app) as c:
        r = c.post("/api/settings", content=raw, headers={"Content-Type": "application/json"})
        assert r.status_code == 422, r.text
        assert "idle_unload_minutes" in r.json()["detail"]
    assert settings.get_idle_unload_minutes() == 20, "nothing changed"


def test_settings_api_takes_a_whole_float():
    from fastapi.testclient import TestClient

    import app.main as main_mod

    with TestClient(main_mod.app) as c:
        r = c.post(
            "/api/settings",
            content='{"idle_unload_minutes": 5.0}',
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 200 and r.json()["idle_unload_minutes"] == 5


@pytest.mark.parametrize("key", ["port", "auto_delete_days", "max_duration_sec"])
@pytest.mark.parametrize("value", ["Infinity", "-Infinity", "1e400"])
def test_settings_api_answers_422_not_500_for_an_infinite_integer(key, value):
    from fastapi.testclient import TestClient

    import app.main as main_mod

    with TestClient(main_mod.app) as c:
        r = c.post(
            "/api/settings",
            content=f'{{"{key}": {value}}}',
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == f"{key} must be an integer"
