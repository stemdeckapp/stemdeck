"""Tests for the on-demand lead/backing vocal split (#275).

Mirrors test_separate_fallback.py's approach: the real subprocess machinery
(Popen, stderr streaming, watchdog, cleanup) runs end-to-end against a stub
worker script swapped in via the _spawn_cmd seam, so no real ML model or
GPU is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core.models import Job
from app.pipeline import vocal_split as vs_mod
from app.pipeline.errors import SeparationError

_SUCCESS_WORKER = """
import sys, os
out_dir = sys.argv[3]
open(os.path.join(out_dir, "lead_vocals.wav"), "wb").write(b"RIFF")
open(os.path.join(out_dir, "backing_vocals.wav"), "wb").write(b"RIFF")
sys.stderr.write("@@DONE@@\\n")
sys.stderr.flush()
"""

_FAILING_WORKER = """
import sys, json
sys.stderr.write("@@ERROR@@" + json.dumps("model download failed: connection reset") + "\\n")
sys.stderr.flush()
sys.exit(1)
"""

# Exits 0 / prints @@DONE@@ but never actually writes the two output files --
# a worker-side bug this should still catch rather than trust blindly.
_LIAR_WORKER = """
import sys
sys.stderr.write("@@DONE@@\\n")
sys.stderr.flush()
"""


def _stub(code: str):
    def fake_spawn(device: str, vocals_path: Path, out_dir: Path) -> list[str]:
        return [sys.executable, "-c", code, device, str(vocals_path), str(out_dir)]

    return fake_spawn


@pytest.fixture()
def stems_dir(tmp_path: Path) -> Path:
    d = tmp_path / "stems"
    d.mkdir()
    (d / "vocals.wav").write_bytes(b"RIFF")
    return d


@pytest.fixture()
def job() -> Job:
    return Job(id="abcdefabc275")


def test_split_vocals_success(job, stems_dir, monkeypatch):
    monkeypatch.setattr(vs_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(vs_mod, "_spawn_cmd", _stub(_SUCCESS_WORKER))

    result = vs_mod.split_vocals(job, stems_dir)

    assert result == ["lead_vocals", "backing_vocals"]
    assert (stems_dir / "lead_vocals.wav").is_file()
    assert (stems_dir / "backing_vocals.wav").is_file()
    # vocals.wav itself must never be touched by the split.
    assert (stems_dir / "vocals.wav").read_bytes() == b"RIFF"


def test_split_vocals_subprocess_failure_raises_with_tail(job, stems_dir, monkeypatch):
    monkeypatch.setattr(vs_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(vs_mod, "_spawn_cmd", _stub(_FAILING_WORKER))

    with pytest.raises(SeparationError) as exc_info:
        vs_mod.split_vocals(job, stems_dir)

    assert "connection reset" in "\n".join(exc_info.value.tail)
    assert not (stems_dir / "lead_vocals.wav").exists()


def test_split_vocals_missing_output_raises(job, stems_dir, monkeypatch):
    """A worker that reports success but didn't actually write the files is a
    bug we catch rather than silently report a successful split."""
    monkeypatch.setattr(vs_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(vs_mod, "_spawn_cmd", _stub(_LIAR_WORKER))

    with pytest.raises(SeparationError):
        vs_mod.split_vocals(job, stems_dir)


def test_split_vocals_requires_vocals_stem(job, tmp_path, monkeypatch):
    empty_stems_dir = tmp_path / "no-vocals-stems"
    empty_stems_dir.mkdir()
    monkeypatch.setattr(vs_mod, "get_demucs_device", lambda: "cpu")

    with pytest.raises(SeparationError, match="vocals.wav"):
        vs_mod.split_vocals(job, empty_stems_dir)
