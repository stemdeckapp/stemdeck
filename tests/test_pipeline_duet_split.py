"""Tests for the on-demand duet split.

Mirrors test_pipeline_vocal_split.py: the real subprocess machinery (Popen,
stderr streaming, watchdog, cleanup) runs end-to-end against a stub worker
script swapped in via the _spawn_cmd seam, so no real ML model or GPU is
needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core.models import Job
from app.pipeline import duet_split as ds_mod
from app.pipeline.errors import SeparationError

_SUCCESS_WORKER = """
import sys, os
out_dir = sys.argv[3]
open(os.path.join(out_dir, "voice_1.wav"), "wb").write(b"RIFF")
open(os.path.join(out_dir, "voice_2.wav"), "wb").write(b"RIFF")
sys.stderr.write("@@DONE@@\\n")
sys.stderr.flush()
"""

_FAILING_WORKER = """
import sys, json
sys.stderr.write("@@ERROR@@" + json.dumps("model download failed: connection reset") + "\\n")
sys.stderr.flush()
sys.exit(1)
"""

# Exits 0 / prints @@DONE@@ but never writes the two output files -- a
# worker-side bug this should still catch rather than trust blindly.
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
    return Job(id="abcdefabc630")


def test_split_duet_success(job, stems_dir, monkeypatch):
    monkeypatch.setattr(ds_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(ds_mod, "_spawn_cmd", _stub(_SUCCESS_WORKER))

    result = ds_mod.split_duet(job, stems_dir)

    assert result == ["voice_1", "voice_2"]
    assert (stems_dir / "voice_1.wav").is_file()
    assert (stems_dir / "voice_2.wav").is_file()


def test_split_duet_missing_vocals(job, tmp_path, monkeypatch):
    empty = tmp_path / "stems"
    empty.mkdir()
    with pytest.raises(SeparationError, match="vocals.wav not found"):
        ds_mod.split_duet(job, empty)


def test_split_duet_worker_failure_surfaces_stderr(job, stems_dir, monkeypatch):
    monkeypatch.setattr(ds_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(ds_mod, "_spawn_cmd", _stub(_FAILING_WORKER))

    with pytest.raises(SeparationError) as exc:
        ds_mod.split_duet(job, stems_dir)
    assert "connection reset" in str(exc.value)


def test_split_duet_rejects_worker_that_wrote_nothing(job, stems_dir, monkeypatch):
    monkeypatch.setattr(ds_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(ds_mod, "_spawn_cmd", _stub(_LIAR_WORKER))

    with pytest.raises(SeparationError, match="did not produce"):
        ds_mod.split_duet(job, stems_dir)


def test_split_duet_leaves_base_stems_untouched(job, stems_dir, monkeypatch):
    monkeypatch.setattr(ds_mod, "get_demucs_device", lambda: "cpu")
    monkeypatch.setattr(ds_mod, "_spawn_cmd", _stub(_SUCCESS_WORKER))

    ds_mod.split_duet(job, stems_dir)

    assert (stems_dir / "vocals.wav").read_bytes() == b"RIFF"
