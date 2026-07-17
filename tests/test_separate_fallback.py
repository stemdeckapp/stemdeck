"""Tests for the GPU->CPU separation fallback (#276).

The demucs invocation is swapped for stub Python one-liners via the
_demucs_cmd seam, so the real process machinery (Popen, stderr streaming,
watchdog, cancel translation) runs end-to-end without demucs or a GPU.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core.models import Job, JobCancelled
from app.pipeline import separate as sep_mod
from app.pipeline.errors import SeparationError


def _stub_cmds(fail_devices: set[str], calls: list[str]):
    """A _demucs_cmd replacement: fails with a CUDA-OOM message on the given
    devices, succeeds (writing a stem WAV where demucs would) elsewhere."""

    def fake_cmd(device: str, source: Path, job_dir: Path) -> list[str]:
        calls.append(device)
        if device in fail_devices:
            code = (
                "import sys;"
                " sys.stderr.write('RuntimeError: CUDA out of memory. Tried 2 GiB\\n');"
                " sys.exit(1)"
            )
        else:
            out_dir = job_dir / sep_mod.DEMUCS_MODEL / source.stem
            code = (
                "import os, sys;"
                f" d = {str(out_dir)!r};"
                " os.makedirs(d, exist_ok=True);"
                " open(os.path.join(d, 'vocals.wav'), 'wb').write(b'RIFF');"
                " sys.stderr.write('100%|separated\\n')"
            )
        return [sys.executable, "-c", code]

    return fake_cmd


@pytest.fixture()
def job(tmp_path: Path):
    j = Job(id="abcdefabc276")
    (tmp_path / "source.wav").write_bytes(b"RIFF")
    return j


def test_gpu_failure_falls_back_to_cpu(job, tmp_path, monkeypatch, caplog):
    import logging

    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    monkeypatch.setattr(sep_mod, "_demucs_cmd", _stub_cmds({"cuda"}, calls))

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


def test_records_startup_timing_on_first_progress_line(job, tmp_path, monkeypatch):
    """#288: measurement only -- time from Popen to the first progress line
    demucs emits, so the real subprocess/model-load startup cost can be
    quantified before deciding whether it's worth a design change."""
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep_mod, "_demucs_cmd", _stub_cmds(set(), calls))

    sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert job.stage_timings is not None
    assert "separate_startup" in job.stage_timings
    assert job.stage_timings["separate_startup"] >= 0.0


def test_gpu_success_needs_no_fallback(job, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    monkeypatch.setattr(sep_mod, "_demucs_cmd", _stub_cmds(set(), calls))

    stems_root = sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert calls == ["cuda"]
    assert stems_root.is_dir()
    assert job.gpu_fallback is False
    assert job.compute_device == "cuda"


def test_cpu_failure_does_not_retry(job, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(sep_mod, "_demucs_cmd", _stub_cmds({"cpu"}, calls))

    with pytest.raises(SeparationError) as exc_info:
        sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert calls == ["cpu"]  # exactly one attempt
    assert job.gpu_fallback is False
    assert exc_info.value.device == "cpu"


def test_both_attempts_failing_raises_with_both_tails(job, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "mps")
    monkeypatch.setattr(sep_mod, "_demucs_cmd", _stub_cmds({"mps", "cpu"}, calls))

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
    monkeypatch.setattr(sep_mod, "_demucs_cmd", _stub_cmds({"cuda"}, calls))
    job.cancel_requested = True  # POST /cancel arrived before/mid attempt

    with pytest.raises(JobCancelled):
        sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert calls == ["cuda"]  # no CPU retry after a cancel


def test_partial_gpu_output_cleared_before_retry(job, tmp_path, monkeypatch):
    """A failed GPU attempt's partial stems must not leak into the CPU run."""
    calls: list[str] = []
    marker = tmp_path / sep_mod.DEMUCS_MODEL / "partial-garbage.wav"

    def fake_cmd(device: str, source: Path, job_dir: Path) -> list[str]:
        calls.append(device)
        if device == "cuda":
            # Simulate demucs dying after writing partial output.
            code = (
                "import os, sys;"
                f" os.makedirs(os.path.dirname({str(marker)!r}), exist_ok=True);"
                f" open({str(marker)!r}, 'wb').write(b'junk');"
                " sys.stderr.write('RuntimeError: CUDA error\\n');"
                " sys.exit(1)"
            )
            return [sys.executable, "-c", code]
        out_dir = tmp_path / sep_mod.DEMUCS_MODEL / source.stem
        code = (
            "import os;"
            f" os.makedirs({str(out_dir)!r}, exist_ok=True);"
            f" open(os.path.join({str(out_dir)!r}, 'vocals.wav'), 'wb').write(b'RIFF')"
        )
        return [sys.executable, "-c", code]

    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: "cuda")
    monkeypatch.setattr(sep_mod, "_demucs_cmd", fake_cmd)

    sep_mod.separate(job, tmp_path / "source.wav", tmp_path)

    assert calls == ["cuda", "cpu"]
    assert not marker.exists(), "partial GPU output must be cleared before the CPU retry"
