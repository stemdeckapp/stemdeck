"""Tearing down a child that will not go quietly, and the peaks cache's
failure paths.

Every worker StemDeck spawns can wedge -- an uninterruptible CUDA call, an
onnxruntime session mid-inference -- and the teardown paths that handle that are
the ones a normal run never touches. They matter because getting them wrong
leaves a process holding the GPU with nobody to collect its result, which is the
failure #519 was about.

The pattern is the same in three modules: terminate, wait briefly, then kill --
*and reap*, because kill() only sends the signal. A worker wedged in an
uninterruptible call otherwise becomes a zombie whose pipes close only
incidentally, whenever the Popen refcount happens to drop.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf


class _StubbornProc:
    """Ignores terminate(); only kill() ends it."""

    def __init__(self):
        self.terminated = False
        self.killed = False
        self.reaped = False
        self._waits = 0

    def poll(self):
        return None if not self.killed else 1

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self._waits += 1
        if not self.killed:
            raise subprocess.TimeoutExpired("worker", timeout or 5)
        return 1

    def communicate(self, timeout=None):
        self.reaped = True
        return b"", b""


class _CompliantProc(_StubbornProc):
    def terminate(self):
        self.terminated = True
        self.killed = True  # exits on the signal

    def wait(self, timeout=None):
        self._waits += 1
        return 0


# --------------------------------------------------------------------------
# separate: the persistent demucs worker
# --------------------------------------------------------------------------


def test_a_wedged_demucs_worker_is_killed_and_reaped(monkeypatch):
    """kill() only sends the signal. Without the reap the worker becomes a
    zombie whose pipes are closed only incidentally."""
    from app.pipeline import separate as sep

    proc = _StubbornProc()
    monkeypatch.setitem(sep._worker, "proc", proc)
    monkeypatch.setitem(sep._worker, "device", "cuda")

    sep._kill_worker()

    assert proc.terminated and proc.killed
    assert proc.reaped, "the killed worker was never reaped"
    assert sep._worker.get("proc") is None


def test_a_worker_that_exits_on_the_signal_is_not_killed(monkeypatch):
    from app.pipeline import separate as sep

    proc = _CompliantProc()
    monkeypatch.setitem(sep._worker, "proc", proc)

    sep._kill_worker()

    assert proc.terminated
    assert proc.reaped is False, "a cooperative worker should not need reaping"


def test_killing_a_worker_that_already_exited_is_harmless(monkeypatch):
    from app.pipeline import separate as sep

    class _Gone(_StubbornProc):
        def poll(self):
            return 0

    proc = _Gone()
    monkeypatch.setitem(sep._worker, "proc", proc)

    sep._kill_worker()

    assert proc.terminated is False
    assert sep._worker.get("proc") is None


def test_killing_when_there_is_no_worker_is_harmless():
    from app.pipeline import separate as sep

    sep._worker.clear()

    sep._kill_worker()  # must not raise


def test_a_separation_that_produced_no_output_dir_is_a_failure(tmp_path, monkeypatch):
    """The worker claimed success but wrote nothing. Returning the path anyway
    would fail later, in a stage that cannot explain why."""
    from app.core.models import Job
    from app.pipeline import separate as sep
    from app.pipeline.errors import SeparationError

    monkeypatch.setattr(sep, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep, "_run_demucs", lambda j, s, d, dev: (0, []))
    (tmp_path / "source.wav").write_bytes(b"RIFF")

    with pytest.raises(SeparationError, match="output not found"):
        sep.separate(Job(id="abcdefabcdef"), tmp_path / "source.wav", tmp_path)


def test_a_machine_without_certifi_still_spawns_the_worker(tmp_path, monkeypatch):
    """certifi is how a frozen build finds a CA bundle. Its absence is a
    source checkout, not an error."""
    from app.core.models import Job
    from app.pipeline import separate as sep

    sep._worker.clear()
    real_import = __import__

    def _no_certifi(name, *a, **kw):
        if name == "certifi":
            raise ModuleNotFoundError("no certifi")
        return real_import(name, *a, **kw)

    monkeypatch.delitem(sys.modules, "certifi", raising=False)
    monkeypatch.setattr("builtins.__import__", _no_certifi)
    monkeypatch.setattr(sep, "get_separation_quality", lambda: "standard")
    monkeypatch.setattr(
        sep,
        "_spawn_worker_cmd",
        lambda device: [sys.executable, "-c", "import sys; sys.stdin.readline(); sys.exit(1)"],
    )
    (tmp_path / "source.wav").write_bytes(b"RIFF")

    code, _tail = sep._run_demucs(Job(id="abcdefabcdef"), tmp_path / "source.wav", tmp_path, "cpu")

    assert code == 1  # it ran and failed on its own terms, not on the import
    sep._kill_worker()


# --------------------------------------------------------------------------
# sections: the analysis child
# --------------------------------------------------------------------------


def test_a_section_process_that_already_exited_is_left_alone():
    from app.pipeline import sections as sec

    class _Gone(_StubbornProc):
        def poll(self):
            return 0

    proc = _Gone()

    sec._terminate(proc)

    assert proc.terminated is False


def test_a_section_process_that_ignores_terminate_is_killed():
    from app.pipeline import sections as sec

    proc = _StubbornProc()

    sec._terminate(proc)

    assert proc.terminated and proc.killed


def test_a_section_process_that_exits_on_the_signal_is_not_killed():
    from app.pipeline import sections as sec

    proc = _CompliantProc()

    sec._terminate(proc)

    assert proc.terminated
    assert proc._waits == 1


# --------------------------------------------------------------------------
# vocal_split: certifi again
# --------------------------------------------------------------------------


def test_a_vocal_split_runs_without_certifi(tmp_path, monkeypatch):
    from app.core.models import Job
    from app.pipeline import vocal_split as vs
    from app.pipeline.errors import SeparationError

    stems = tmp_path / "stems"
    stems.mkdir()
    (stems / "vocals.wav").write_bytes(b"RIFF")

    real_import = __import__

    def _no_certifi(name, *a, **kw):
        if name == "certifi":
            raise ModuleNotFoundError("no certifi")
        return real_import(name, *a, **kw)

    monkeypatch.delitem(sys.modules, "certifi", raising=False)
    monkeypatch.setattr("builtins.__import__", _no_certifi)
    monkeypatch.setattr(vs, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(
        vs,
        "_spawn_cmd",
        lambda d, v, o: [sys.executable, "-c", "import sys; sys.exit(1)"],
    )

    with pytest.raises(SeparationError):
        vs.split_vocals(Job(id="abcdefabc275"), stems)


# --------------------------------------------------------------------------
# collect: the peaks cache is best-effort throughout
# --------------------------------------------------------------------------


def _stem(path: Path, seconds=0.2):
    t = np.linspace(0, seconds, int(8000 * seconds), endpoint=False)
    sf.write(str(path), (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), 8000)
    return path


def test_a_stem_that_cannot_be_scanned_is_skipped_not_fatal(tmp_path, caplog):
    """A truncated WAV costs that lane its cached waveform, which the client
    then decodes itself. It must not cost the job."""
    from app.pipeline.collect import compute_stem_peaks

    _stem(tmp_path / "vocals.wav")
    (tmp_path / "drums.wav").write_bytes(b"not a wav")

    rms = compute_stem_peaks(tmp_path, ["vocals", "drums"])

    assert "vocals" in rms
    assert "drums" not in rms
    assert "could not compute peaks" in caplog.text
    # The stems that did scan were still written.
    assert (tmp_path / "peaks.json").is_file()


def test_peaks_that_cannot_be_written_do_not_fail_the_job(tmp_path, monkeypatch, caplog):
    """Missing peaks.json degrades to client-side decode; the stems are fine."""
    from app.pipeline.collect import compute_stem_peaks

    _stem(tmp_path / "vocals.wav")

    def _boom(self, *a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "write_text", _boom)

    rms = compute_stem_peaks(tmp_path, ["vocals"])

    assert "vocals" in rms, "the scan result was lost along with the write"
    assert "could not write peaks.json" in caplog.text


def test_an_unreadable_existing_peaks_file_is_replaced_not_fatal(tmp_path, caplog):
    """merge_stem_peaks adds the split's lanes to what is already there. A
    corrupt file costs the existing lanes their cache, not the new ones."""
    from app.pipeline.collect import merge_stem_peaks

    _stem(tmp_path / "lead_vocals.wav")
    (tmp_path / "peaks.json").write_text("{truncated", encoding="utf-8")

    rms = merge_stem_peaks(tmp_path, ["lead_vocals"])

    assert "lead_vocals" in rms
    assert "could not read existing peaks.json" in caplog.text
    assert json.loads((tmp_path / "peaks.json").read_text(encoding="utf-8"))


def test_merged_peaks_that_cannot_be_written_do_not_fail_the_split(tmp_path, monkeypatch, caplog):
    from app.pipeline.collect import merge_stem_peaks

    _stem(tmp_path / "lead_vocals.wav")

    def _boom(self, *a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", _boom)

    rms = merge_stem_peaks(tmp_path, ["lead_vocals"])

    assert "lead_vocals" in rms
    assert "could not write peaks.json" in caplog.text


def test_merging_keeps_the_lanes_that_were_already_cached(tmp_path):
    from app.pipeline.collect import merge_stem_peaks

    _stem(tmp_path / "lead_vocals.wav")
    (tmp_path / "peaks.json").write_text(json.dumps({"drums": [[-1.0, 1.0]]}), encoding="utf-8")

    merge_stem_peaks(tmp_path, ["lead_vocals"])

    written = json.loads((tmp_path / "peaks.json").read_text(encoding="utf-8"))
    assert "drums" in written, "an existing lane lost its cached waveform"
    assert "lead_vocals" in written
