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
    bd.reset_model_cache()
    yield
    sep_mod._kill_worker()
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
    # And once it is quiet, the same check unloads.
    assert "separation worker" in await idle_unload.check()


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
        assert r.json()["detail"] == "idle_unload_minutes must be an integer"
        r = c.post("/api/settings", json={"idle_unload_minutes": None})
        assert r.status_code == 422
        assert c.get("/api/settings").json()["idle_unload_minutes"] == 1440, "nothing changed"
