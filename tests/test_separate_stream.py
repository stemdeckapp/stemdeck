"""How the parent reads the demucs worker's stderr.

test_separate_fallback.py drives the happy path and the GPU->CPU fallback
through the _spawn_worker_cmd seam. What it leaves uncovered is the reader's
own handling of what a worker can actually emit: tqdm's carriage returns, log
lines mixed in with progress, a tail that must not grow without bound, an
@@ERROR@@ payload that is not JSON, and an EOF with nothing said at all.

Plus the dispatch race, which is real rather than hypothetical: a cancel on the
previous job can kill the shared worker after _get_worker() handed it over and
before this dispatch reaches its stdin.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core.models import Job
from app.pipeline import separate as sep_mod


@pytest.fixture
def job(tmp_path: Path):
    (tmp_path / "source.wav").write_bytes(b"RIFF")
    return Job(id="abcdefabc276")


@pytest.fixture(autouse=True)
def _reset_worker():
    sep_mod._worker.clear()
    yield
    sep_mod._kill_worker()


@pytest.fixture(autouse=True)
def _cpu(monkeypatch):
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep_mod, "get_separation_quality", lambda: "standard")


def _use(monkeypatch, code: str):
    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", lambda device: [sys.executable, "-c", code])


def _run(job, tmp_path):
    return sep_mod._run_demucs(job, tmp_path / "source.wav", tmp_path, "cpu")


# --------------------------------------------------------------------------
# progress parsing
# --------------------------------------------------------------------------


_TQDM_WORKER = r"""
import sys
sys.stdin.readline()
# tqdm redraws in place with \r and emits no newline until it is finished.
sys.stderr.write(" 10%|##        | 1/10\r")
sys.stderr.write(" 55%|#####     | 5/10\r")
sys.stderr.write("100%|##########| 10/10\r")
sys.stderr.write("@@DONE@@\n")
sys.stderr.flush()
"""


def test_carriage_returned_progress_is_read_as_progress(job, tmp_path, monkeypatch):
    """demucs reports through tqdm. Splitting on newlines alone would show no
    movement at all for the longest stage in the pipeline."""
    _use(monkeypatch, _TQDM_WORKER)

    code, _tail = _run(job, tmp_path)

    assert code == 0
    assert job.progress == 1.0
    assert "Separating" in job.stage_message


_OUT_OF_RANGE_WORKER = r"""
import sys
sys.stdin.readline()
sys.stderr.write("120%\r")
sys.stderr.write("@@DONE@@\n")
sys.stderr.flush()
"""


def test_a_percentage_past_one_hundred_is_clamped(job, tmp_path, monkeypatch):
    """A progress bar that overshoots would render past the end of its track."""
    _use(monkeypatch, _OUT_OF_RANGE_WORKER)

    _run(job, tmp_path)

    assert job.progress == 1.0


_NOISY_WORKER = r"""
import sys
sys.stdin.readline()
sys.stderr.write("Selected model is a bag of 1 models\n")
sys.stderr.write(" 50%|#####     | 5/10\r")
sys.stderr.write("Separated tracks will be stored in /tmp\n")
sys.stderr.write("@@ERROR@@\"something went wrong\"\n")
sys.stderr.flush()
sys.exit(1)
"""


def test_log_lines_are_kept_as_diagnostics_while_progress_is_not(job, tmp_path, monkeypatch):
    """The tail is what surfaces on a failure. Filling it with progress redraws
    would push the actual error out of it."""
    _use(monkeypatch, _NOISY_WORKER)

    code, tail = _run(job, tmp_path)

    assert code == 1
    assert "Selected model is a bag of 1 models" in tail
    assert "something went wrong" in tail
    assert not any("50%" in line for line in tail)


_CHATTY_WORKER = r"""
import sys
sys.stdin.readline()
for i in range(200):
    sys.stderr.write("log line %d\n" % i)
sys.stderr.write("@@ERROR@@\"gave up\"\n")
sys.stderr.flush()
sys.exit(1)
"""


def test_the_diagnostic_tail_does_not_grow_without_bound(job, tmp_path, monkeypatch):
    """A worker that logs per chunk would otherwise pin its whole output in
    memory and put all of it in the job's error record."""
    _use(monkeypatch, _CHATTY_WORKER)

    _code, tail = _run(job, tmp_path)

    assert len(tail) <= 41
    # It keeps the end, which is where the failure is.
    assert "gave up" in tail[-1]
    assert "log line 0" not in tail


_BARE_ERROR_WORKER = r"""
import sys
sys.stdin.readline()
sys.stderr.write("@@ERROR@@not json at all\n")
sys.stderr.flush()
sys.exit(1)
"""


def test_an_error_payload_that_is_not_json_is_still_reported(job, tmp_path, monkeypatch):
    _use(monkeypatch, _BARE_ERROR_WORKER)

    code, tail = _run(job, tmp_path)

    assert code == 1
    assert "not json at all" in tail[-1]


_SILENT_WORKER = r"""
import sys
sys.stdin.readline()
sys.exit(1)
"""


def test_a_worker_that_dies_saying_nothing_is_a_failure(job, tmp_path, monkeypatch):
    """EOF with no marker is a crash. Reading it as success would mark a job
    done with no stems on disk."""
    _use(monkeypatch, _SILENT_WORKER)

    code, _tail = _run(job, tmp_path)

    assert code == 1


_BLANK_LINE_WORKER = r"""
import sys
sys.stdin.readline()
sys.stderr.write("\n\r   \r\n")
sys.stderr.write("@@DONE@@\n")
sys.stderr.flush()
"""


def test_blank_redraws_are_skipped_rather_than_collected(job, tmp_path, monkeypatch):
    """tqdm emits bare carriage returns between frames; each one would
    otherwise become an empty diagnostic line."""
    _use(monkeypatch, _BLANK_LINE_WORKER)

    code, tail = _run(job, tmp_path)

    assert code == 0
    assert tail == []


# --------------------------------------------------------------------------
# worker reuse and the dispatch race
# --------------------------------------------------------------------------


def test_a_worker_that_died_before_dispatch_is_an_ordinary_failure(job, tmp_path, monkeypatch):
    """A cancel on the previous job can kill the shared worker between
    _get_worker() returning it and this write reaching its stdin. The retry
    policy in separate() handles it exactly like a non-zero exit -- but only if
    the broken pipe is caught rather than raised."""

    class _DeadStdin:
        def write(self, _data):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            pass

    class _DeadProc:
        stdin = _DeadStdin()
        stderr = object()

        def poll(self):
            return None

    monkeypatch.setattr(sep_mod, "_get_worker", lambda device: _DeadProc())
    monkeypatch.setattr(sep_mod, "set_proc", lambda *a, **kw: None)
    killed = []
    monkeypatch.setattr(sep_mod, "_kill_worker", lambda: killed.append(True))

    code, tail = _run(job, tmp_path)

    assert code == 1
    assert "not accepting input" in tail[0]
    assert killed, "the dead worker was left in place for the next job"


def test_a_worker_with_no_pipes_is_refused(job, tmp_path, monkeypatch):
    class _NoPipes:
        stdin = None
        stderr = None

        def poll(self):
            return None

    monkeypatch.setattr(sep_mod, "_get_worker", lambda device: _NoPipes())

    with pytest.raises(RuntimeError, match="no stdin/stderr pipe"):
        _run(job, tmp_path)


_ECHO_SHIFTS_WORKER = r"""
import sys, json
req = json.loads(sys.stdin.readline())
sys.stderr.write("shifts=%s\n" % req["shifts"])
sys.stderr.write("@@ERROR@@\"stop\"\n")
sys.stderr.flush()
sys.exit(1)
"""


def test_the_quality_setting_reaches_the_worker_as_shifts(job, tmp_path, monkeypatch):
    """shifts is what "best" actually buys; dropping it makes the two presets
    identical while still costing the user the wait they chose."""
    monkeypatch.setattr(sep_mod, "get_separation_quality", lambda: "best")
    _use(monkeypatch, _ECHO_SHIFTS_WORKER)

    _code, tail = _run(job, tmp_path)

    assert "shifts=2" in tail[0]


def test_standard_quality_asks_for_a_single_pass(job, tmp_path, monkeypatch):
    monkeypatch.setattr(sep_mod, "get_separation_quality", lambda: "standard")
    _use(monkeypatch, _ECHO_SHIFTS_WORKER)

    _code, tail = _run(job, tmp_path)

    assert "shifts=1" in tail[0]


def test_a_failed_job_never_leaves_the_worker_warm(job, tmp_path, monkeypatch):
    """After a failure mid-inference the CUDA state is not something the next
    job can trust, so the worker must not be reused (#514)."""
    _use(monkeypatch, _BARE_ERROR_WORKER)

    _run(job, tmp_path)

    assert sep_mod._worker.get("proc") is None


def test_the_source_and_job_dir_are_handed_to_the_worker(job, tmp_path, monkeypatch):
    echo = r"""
import sys, json
req = json.loads(sys.stdin.readline())
sys.stderr.write("src=%s\n" % req["source"])
sys.stderr.write("dir=%s\n" % req["job_dir"])
sys.stderr.write("@@ERROR@@\"stop\"\n")
sys.stderr.flush()
sys.exit(1)
"""
    _use(monkeypatch, echo)

    _code, tail = _run(job, tmp_path)

    assert f"src={tmp_path / 'source.wav'}" in tail[0]
    assert f"dir={tmp_path}" in tail[1]
