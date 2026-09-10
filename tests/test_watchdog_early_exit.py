"""The stall watchdogs' other exit: the worker finished on its own.

Both long separation stages run a watchdog thread whose only job is to kill a
worker that has stopped producing output. It wakes every 30 s, and the first
thing it does on waking is ask whether the process is still there -- because a
worker that has already exited is not stalled, and terminating a reaped pid is
either a no-op or, once the pid has been recycled, a signal sent to something
else entirely.

The 30 s sleep is what makes this awkward to reach in a test, so Event.wait is
replaced with one that expires immediately -- but only after the real worker
has exited, so the poll the watchdog then makes is decisive rather than a race.
"""

from __future__ import annotations

import sys
import threading

import pytest
import soundfile as sf

from app.core.models import Job
from app.pipeline.errors import SeparationError

JOB = "abcdefabcdef"

# Reads the request line the parent writes, then exits without answering. The
# reader loop sees EOF and unwinds; the watchdog sees a dead process.
_QUITTER = "import sys; sys.stdin.readline(); sys.exit(7)"  # answers nothing
_QUITS_IMMEDIATELY = "import sys; sys.exit(3)"


@pytest.fixture
def wake_after_the_worker_exits(monkeypatch):
    """Event.wait expires once -- after the captured process has exited -- then
    reports the event set so the watchdog loop ends.

    The test fills `holder["proc"]`; without it the first expiry is a plain
    race, which is what makes this branch flaky to reach otherwise.
    """
    holder: dict[str, object] = {}
    real = threading.Event.wait
    seen: dict[int, int] = {}

    def _wait(self, timeout=None):
        if timeout is None:
            return real(self, timeout)
        n = seen[id(self)] = seen.get(id(self), 0) + 1
        if n > 1:
            return True
        proc = holder.get("proc")
        if proc is not None:
            proc.wait()
        return False

    monkeypatch.setattr(threading.Event, "wait", _wait)
    return holder


def _capture_proc(monkeypatch, module, holder):
    """Tee the module's set_proc so the fixture can wait on the real child."""
    real = module.set_proc

    def _set(job_id, proc):
        if proc is not None:
            holder["proc"] = proc
        return real(job_id, proc)

    monkeypatch.setattr(module, "set_proc", _set)


def test_the_demucs_watchdog_stands_down_when_the_worker_already_exited(
    tmp_path, monkeypatch, wake_after_the_worker_exits, caplog
):
    """A crashed worker unwinds through the reader loop, not the watchdog. If
    the watchdog took the stall path here it would log a stall for every
    ordinary failure and terminate a pid it does not own."""
    from app.pipeline import separate as sep

    sep._worker.clear()
    monkeypatch.setattr(sep, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep, "get_separation_quality", lambda: "standard")
    monkeypatch.setattr(sep, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", _QUITTER])
    _capture_proc(monkeypatch, sep, wake_after_the_worker_exits)
    (tmp_path / "source.wav").write_bytes(b"RIFF")

    try:
        code, _tail = sep._run_demucs(Job(id=JOB), tmp_path / "source.wav", tmp_path, "cpu")
    finally:
        sep._kill_worker()

    assert code == 1, "EOF from a dead worker is an ordinary failure for separate()"
    assert "stalled" not in caplog.text


def test_the_vocal_split_watchdog_stands_down_when_the_worker_already_exited(
    tmp_path, monkeypatch, wake_after_the_worker_exits, caplog
):
    """Same branch on the other stage. audio-separator exiting on a missing
    model download is the common way to get here."""
    from app.pipeline import vocal_split as vs

    stems = tmp_path / "stems"
    stems.mkdir()
    sf.write(str(stems / "vocals.wav"), [0.0] * 800, 8000)

    monkeypatch.setattr(vs, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(vs, "_spawn_cmd", lambda *a: [sys.executable, "-c", _QUITS_IMMEDIATELY])
    _capture_proc(monkeypatch, vs, wake_after_the_worker_exits)

    with pytest.raises(SeparationError):
        vs.split_vocals(Job(id=JOB), stems)

    assert "vocal split failed" in caplog.text
    assert "stalled" not in caplog.text


# --------------------------------------------------------------------------
# the startup timing, recorded on the first progress line
# --------------------------------------------------------------------------


_REPORTS_PROGRESS = (
    "import sys; sys.stdin.readline(); "
    "sys.stderr.write(' 42%|#### |\\n@@DONE@@\\n'); sys.stderr.flush()"
)


def test_the_startup_timing_is_added_to_the_laps_already_on_the_job(tmp_path, monkeypatch):
    """stage_timings already holds the download and analyze laps by the time
    separation reports its first percentage. Replacing the map rather than
    adding to it would drop them from the job record, which is the only place
    a slow import can be attributed to a stage afterwards."""
    from app.pipeline import separate as sep

    sep._worker.clear()
    monkeypatch.setattr(sep, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep, "get_separation_quality", lambda: "standard")
    monkeypatch.setattr(
        sep, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", _REPORTS_PROGRESS]
    )
    (tmp_path / "source.wav").write_bytes(b"RIFF")

    job = Job(id=JOB)
    job.stage_timings = {"download": 3.2, "analyze": 1.1}

    try:
        code, _tail = sep._run_demucs(job, tmp_path / "source.wav", tmp_path, "cpu")
    finally:
        sep._kill_worker()

    assert code == 0
    assert job.stage_timings["download"] == 3.2, "the earlier laps were thrown away"
    assert job.stage_timings["analyze"] == 1.1
    assert "separate_startup" in job.stage_timings


def test_the_startup_timing_creates_the_map_when_there_is_none(tmp_path, monkeypatch):
    """The other arm: a job whose earlier stages recorded nothing (a resumed
    job, or a local upload that skipped the download) starts with no map at
    all."""
    from app.pipeline import separate as sep

    sep._worker.clear()
    monkeypatch.setattr(sep, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep, "get_separation_quality", lambda: "standard")
    monkeypatch.setattr(
        sep, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", _REPORTS_PROGRESS]
    )
    (tmp_path / "source.wav").write_bytes(b"RIFF")

    job = Job(id=JOB)
    assert job.stage_timings is None

    try:
        sep._run_demucs(job, tmp_path / "source.wav", tmp_path, "cpu")
    finally:
        sep._kill_worker()

    assert list(job.stage_timings) == ["separate_startup"]
