"""The remaining reachable branches: stall watchdogs, quarantine, and odds.

The three stall watchdogs (demucs, vocal split, section analysis) are the same
shape in three modules: a daemon thread that terminates a child which has
stopped producing output. They are the only bound on a worker that has hung
without exiting, and none of them was reachable without making the poll instant
-- the wait interval is an inline 30.

The quarantine path matters for a different reason: it runs when a job has
already failed, so anything it raises replaces the real error with a cleanup
one, and the evidence the user was going to report is gone.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from app.core.models import Job, JobCancelled

# --------------------------------------------------------------------------
# a poll that returns immediately, so a 30-second wait becomes reachable
# --------------------------------------------------------------------------


@pytest.fixture
def instant_wait(monkeypatch):
    """Event.wait returns False once (so the watchdog body runs) then True (so
    the loop ends). Scoped to the test, and the real Event is otherwise intact."""
    real = threading.Event.wait
    seen: dict[int, int] = {}

    def _wait(self, timeout=None):
        if timeout is None:
            return real(self, timeout)
        # Counted per Event, so the watchdog's own first poll always expires
        # regardless of what any other thread is waiting on.
        n = seen[id(self)] = seen.get(id(self), 0) + 1
        return n > 1

    monkeypatch.setattr(threading.Event, "wait", _wait)
    return seen


# --------------------------------------------------------------------------
# the demucs stall watchdog
# --------------------------------------------------------------------------


_SLEEPER = "import sys, time; sys.stdin.readline(); time.sleep(30)"


def test_a_demucs_worker_that_stops_producing_output_is_terminated(
    tmp_path, monkeypatch, instant_wait, caplog
):
    """The only bound on a worker that has hung without exiting. Without it a
    wedged separation holds the GPU until the process dies."""
    from app.pipeline import separate as sep

    sep._worker.clear()
    monkeypatch.setattr(sep, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep, "get_separation_quality", lambda: "standard")
    monkeypatch.setattr(sep, "TIMEOUT_DEMUCS_STALL", -1)  # anything counts as stalled
    monkeypatch.setattr(sep, "_spawn_worker_cmd", lambda d: [sys.executable, "-c", _SLEEPER])
    (tmp_path / "source.wav").write_bytes(b"RIFF")

    try:
        code, _tail = sep._run_demucs(
            Job(id="abcdefabcdef"), tmp_path / "source.wav", tmp_path, "cpu"
        )
    finally:
        sep._kill_worker()

    assert code == 1
    assert "stalled" in caplog.text


def test_a_demucs_worker_that_already_exited_is_left_to_the_reader(
    tmp_path, monkeypatch, instant_wait
):
    """The watchdog returns rather than terminating a process that is already
    gone -- the read loop's EOF is what reports the outcome."""
    from app.pipeline import separate as sep

    sep._worker.clear()
    monkeypatch.setattr(sep, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep, "get_separation_quality", lambda: "standard")
    monkeypatch.setattr(sep, "TIMEOUT_DEMUCS_STALL", 999)
    monkeypatch.setattr(
        sep,
        "_spawn_worker_cmd",
        lambda d: [sys.executable, "-c", "import sys; sys.stdin.readline(); sys.exit(3)"],
    )
    (tmp_path / "source.wav").write_bytes(b"RIFF")

    try:
        code, _tail = sep._run_demucs(
            Job(id="abcdefabcdef"), tmp_path / "source.wav", tmp_path, "cpu"
        )
    finally:
        sep._kill_worker()

    assert code == 1


# --------------------------------------------------------------------------
# the vocal-split stall watchdog
# --------------------------------------------------------------------------


def test_a_vocal_split_that_stops_producing_output_is_terminated(
    tmp_path, monkeypatch, instant_wait, caplog
):
    """onnxruntime reads nothing from stdin mid-inference, so EOF never arrives
    and this is the only thing that bounds it."""
    from app.pipeline import vocal_split as vs
    from app.pipeline.errors import SeparationError

    stems = tmp_path / "stems"
    stems.mkdir()
    (stems / "vocals.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(vs, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(vs, "TIMEOUT_VOCAL_SPLIT", -1)
    monkeypatch.setattr(
        vs, "_spawn_cmd", lambda d, v, o: [sys.executable, "-c", "import time; time.sleep(30)"]
    )

    with pytest.raises(SeparationError):
        vs.split_vocals(Job(id="abcdefabc275"), stems)

    assert "stalled" in caplog.text


def test_a_vocal_split_worker_that_will_not_die_is_killed(tmp_path, monkeypatch):
    """The finally block terminates, waits, then kills. A worker holding the GPU
    past the terminate has to be forced."""
    from app.pipeline import vocal_split as vs
    from app.pipeline.errors import SeparationError

    stems = tmp_path / "stems"
    stems.mkdir()
    (stems / "vocals.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(vs, "get_demucs_device", lambda: "cpu")

    # Ignores SIGTERM, so only kill() ends it.
    ignores_term = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stderr.write('@@ERROR@@\"stop\"\\n'); sys.stderr.flush()\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setattr(vs, "_spawn_cmd", lambda d, v, o: [sys.executable, "-c", ignores_term])

    real_wait = subprocess.Popen.wait

    def _impatient(self, timeout=None):
        # Force the kill branch rather than waiting out the real 5 s window.
        if timeout:
            raise subprocess.TimeoutExpired("worker", timeout)
        return real_wait(self)

    monkeypatch.setattr(subprocess.Popen, "wait", _impatient)

    with pytest.raises(SeparationError):
        vs.split_vocals(Job(id="abcdefabc275"), stems)


# --------------------------------------------------------------------------
# the section-analysis child
# --------------------------------------------------------------------------


def test_a_section_child_with_no_pipes_is_torn_down(monkeypatch):
    """Popen was asked for pipes; not getting them means something is wrong
    enough that the child must not be left running."""
    from app.pipeline import sections as sec

    class _NoPipes:
        stdout = None
        stderr = None

        def poll(self):
            return None

    killed = []
    monkeypatch.setattr(sec.subprocess, "Popen", lambda *a, **kw: _NoPipes())
    monkeypatch.setattr(sec, "set_proc", lambda *a, **kw: None)
    monkeypatch.setattr(sec, "_terminate", lambda p: killed.append(True))

    with pytest.raises(RuntimeError, match="no output pipes"):
        sec._run_registered_process(Job(id="abcdefabcdef"), ["true"])

    assert killed, "the child was left running"


def test_a_section_child_that_stops_talking_is_terminated(monkeypatch, instant_wait, caplog):
    """A CPU inference pass can take minutes, so silence alone is not a hang --
    the stall timeout is what separates the two."""
    from app.pipeline import sections as sec

    monkeypatch.setattr(sec, "TIMEOUT_SECTIONS_STALL", -1)
    code, _out, _err = sec._run_registered_process(
        Job(id="abcdefabcdef"), [sys.executable, "-c", "import time; time.sleep(30)"]
    )

    assert code != 0
    assert "stalled" in caplog.text


def test_a_section_child_collects_both_streams(monkeypatch):
    """stdout carries the one JSON line, stderr the diagnostics; the reader
    threads must keep them apart."""
    from app.pipeline import sections as sec

    code, out, err = sec._run_registered_process(
        Job(id="abcdefabcdef"),
        [sys.executable, "-c", "import sys; print('on-stdout'); sys.stderr.write('on-stderr\\n')"],
    )

    assert code == 0
    assert out == ["on-stdout"]
    assert any("on-stderr" in line for line in err)


def test_cancelling_a_section_pass_raises_rather_than_returning(monkeypatch):
    """The runner treats a cancelled job differently from a failed one; a plain
    non-zero exit would be quarantined as a failure."""
    from app.pipeline import sections as sec

    job = Job(id="abcdefabcdef")
    job.cancel_requested = True

    with pytest.raises(JobCancelled):
        sec._run_registered_process(job, [sys.executable, "-c", "import time; time.sleep(5)"])


# --------------------------------------------------------------------------
# the failure quarantine
# --------------------------------------------------------------------------


def test_a_quarantine_replaces_an_older_one_for_the_same_job(tmp_path, monkeypatch):
    """A job retried and failed twice: the second attempt's evidence is the one
    worth keeping."""
    from app.pipeline import runner

    job_dir = tmp_path / "abcdefabcdef"
    job_dir.mkdir()
    (job_dir / "error.txt").write_text("second attempt", encoding="utf-8")
    stale = tmp_path / "failed" / "abcdefabcdef"
    stale.mkdir(parents=True)
    (stale / "error.txt").write_text("first attempt", encoding="utf-8")

    runner._quarantine_failed_job(Job(id="abcdefabcdef"), job_dir, tmp_path, RuntimeError("boom"))

    kept = (tmp_path / "failed" / "abcdefabcdef" / "error.txt").read_text(encoding="utf-8")
    # The stage writes its own failure record over the job's; what matters is
    # that the previous attempt's quarantine was cleared rather than merged.
    assert "first attempt" not in kept
    assert "job: abcdefabcdef" in kept


def test_a_quarantine_that_fails_still_removes_the_job_directory(tmp_path, monkeypatch, caplog):
    """It runs after a job already failed. Raising here would replace the real
    error with a cleanup one, and leaving the directory would let restore()
    re-adopt a broken job on the next start."""
    import shutil

    from app.pipeline import runner

    job_dir = tmp_path / "abcdefabcdef"
    job_dir.mkdir()
    (job_dir / "error.txt").write_text("boom", encoding="utf-8")

    def _boom(src, dst):
        raise OSError("cross-device link")

    monkeypatch.setattr(shutil, "move", _boom)

    runner._quarantine_failed_job(
        Job(id="abcdefabcdef"), job_dir, tmp_path, RuntimeError("boom")
    )  # must not raise

    assert "quarantine failed" in caplog.text
    assert not job_dir.exists()


def test_an_ffmpeg_that_overruns_its_timeout_is_killed_and_the_error_surfaces(
    tmp_path, monkeypatch
):
    """The registration exists so a cancel can reach it; the timeout is what
    bounds a hung encoder. Re-raising is deliberate -- the caller decides."""
    from app.pipeline import runner

    with pytest.raises(subprocess.TimeoutExpired):
        runner._run_registered_ffmpeg(
            Job(id="abcdefabcdef"),
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.3,
        )

    from app.core.registry import _procs

    assert _procs.get("abcdefabcdef") is None, "the killed process stayed registered"


# --------------------------------------------------------------------------
# odds
# --------------------------------------------------------------------------


def test_an_ipv6_address_is_never_offered_as_a_lan_address():
    """A link-local IPv6 address needs a zone index and will not work in a
    browser, so showing one is worse than showing nothing."""
    import app.main as main

    assert main._is_lan_ipv4("fe80::1") is False
    assert main._is_lan_ipv4("::1") is False


def test_the_auto_config_range_is_not_a_lan_address():
    """169.254.x means DHCP failed; nothing else can reach it."""
    import app.main as main

    assert main._is_lan_ipv4("169.254.1.1") is False
    assert main._is_lan_ipv4("127.0.0.1") is False
    assert main._is_lan_ipv4("192.168.1.50") is True


def test_a_log_line_whose_date_is_not_a_real_one_has_no_time():
    """strptime raises on a date matching the shape but not the calendar."""
    import app.main as main

    assert main._line_time("2026-02-30 25:61:99 I stemdeck nope") is None


def test_a_recovered_job_with_a_corrupt_metadata_sidecar_still_recovers(tmp_path):
    """The stems are the valuable part; an unreadable sidecar costs the title."""
    from app.core import registry

    stems = tmp_path / "abcdefabcdef" / "stems"
    stems.mkdir(parents=True)
    (stems / "vocals.wav").write_bytes(b"RIFF")
    (tmp_path / "abcdefabcdef" / "metadata.json").write_text("{truncated", encoding="utf-8")

    registry._jobs.clear()
    try:
        registry.restore(tmp_path)
        assert "abcdefabcdef" in registry.all_jobs()
    finally:
        registry._jobs.clear()


def test_the_section_worker_emits_a_heartbeat(capsys):
    """A CPU pass produces no output for minutes; the parent reads this to tell
    "slow" from "hung"."""
    from app.pipeline import section_worker

    stop = threading.Event()
    monkey_interval = 0.01
    original = section_worker._HEARTBEAT_SECONDS
    section_worker._HEARTBEAT_SECONDS = monkey_interval
    try:
        thread = threading.Thread(target=section_worker._heartbeat, args=(stop,), daemon=True)
        thread.start()
        time.sleep(0.05)
        stop.set()
        thread.join(timeout=2)
    finally:
        section_worker._HEARTBEAT_SECONDS = original

    assert "SECTION_HEARTBEAT" in capsys.readouterr().err
