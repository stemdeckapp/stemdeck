"""The parent half of the vocal-split protocol, and the setup wizard's warmup.

test_pipeline_vocal_split.py established the stub-worker harness and covers the
three main outcomes. The branches left over are the ones that only show up when
a worker misbehaves: a stderr tail longer than the cap, an @@ERROR@@ payload
that is not JSON, half the expected output, and no output at all.

warmup.py was at 64%: main() was covered but not one of the four steps it runs,
so nothing checked the thing its docstring is mostly about -- that the two
models known to poison their own cache load through load_or_heal (#502), and
that the section step opts out of Hugging Face symlinks the same way the real
worker does.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from app.core.models import Job
from app.pipeline import vocal_split as vs_mod
from app.pipeline.errors import SeparationError


@pytest.fixture
def stems_dir(tmp_path: Path) -> Path:
    d = tmp_path / "stems"
    d.mkdir()
    (d / "vocals.wav").write_bytes(b"RIFF")
    return d


@pytest.fixture
def job() -> Job:
    return Job(id="abcdefabc275")


def _stub(code: str):
    def fake_spawn(device: str, vocals_path: Path, out_dir: Path) -> list[str]:
        return [sys.executable, "-c", code, device, str(vocals_path), str(out_dir)]

    return fake_spawn


@pytest.fixture(autouse=True)
def _cpu_device(monkeypatch):
    monkeypatch.setattr(vs_mod, "get_demucs_device", lambda: "cpu")


# --------------------------------------------------------------------------
# how the worker is invoked
# --------------------------------------------------------------------------


def test_the_worker_is_spawned_as_a_module_with_its_three_arguments(tmp_path):
    """Spawning by module rather than by path is what makes the worker importable
    from a frozen desktop build."""
    cmd = vs_mod._spawn_cmd("cuda", tmp_path / "vocals.wav", tmp_path / "out")

    assert cmd[0] == sys.executable
    assert cmd[1:3] == ["-m", "app.pipeline.vocal_split_worker"]
    assert cmd[3] == "cuda"
    assert cmd[4].endswith("vocals.wav")
    assert cmd[5].endswith("out")


def test_a_job_with_no_vocals_stem_is_refused_before_spawning_anything(job, tmp_path):
    empty = tmp_path / "stems"
    empty.mkdir()

    with pytest.raises(SeparationError, match="vocals.wav not found"):
        vs_mod.split_vocals(job, empty)


_ENV_ECHO_WORKER = """
import os, sys
sys.stderr.write("PARENT=%s\\n" % os.environ.get("STEMDECK_PARENT_PID", ""))
sys.stderr.write("IOENC=%s\\n" % os.environ.get("PYTHONIOENCODING", ""))
sys.stderr.write("@@ERROR@@nope\\n")
sys.exit(1)
"""


def test_the_child_is_told_who_its_parent_is_and_how_to_encode(job, stems_dir, monkeypatch):
    """The worker arms a watchdog on STEMDECK_PARENT_PID, so a Force-Quit cannot
    leave an onnxruntime process holding the GPU (#519). PYTHONIOENCODING keeps
    a Windows child from writing cp1252 at a parent decoding utf-8."""
    import os

    monkeypatch.setattr(vs_mod, "_spawn_cmd", _stub(_ENV_ECHO_WORKER))

    with pytest.raises(SeparationError) as exc:
        vs_mod.split_vocals(job, stems_dir)

    tail = "\n".join(exc.value.tail or [])
    assert f"PARENT={os.getpid()}" in tail
    assert "IOENC=utf-8:replace" in tail


# --------------------------------------------------------------------------
# reading the worker's stderr
# --------------------------------------------------------------------------


_CHATTY_WORKER = """
import sys
for i in range(200):
    sys.stderr.write("progress line %d\\n" % i)
sys.stderr.write("@@ERROR@@\\"gave up\\"\\n")
sys.stderr.flush()
sys.exit(1)
"""


def test_a_very_chatty_worker_does_not_grow_the_tail_without_bound(job, stems_dir, monkeypatch):
    """The tail is diagnostic context attached to the error. A model that prints
    a progress line per chunk would otherwise pin the whole log in memory and
    put it in the error record."""
    monkeypatch.setattr(vs_mod, "_spawn_cmd", _stub(_CHATTY_WORKER))

    with pytest.raises(SeparationError) as exc:
        vs_mod.split_vocals(job, stems_dir)

    assert len(exc.value.tail) <= 41
    # It keeps the *end* of the output, which is where the failure is.
    assert "gave up" in "\n".join(exc.value.tail)
    assert "progress line 0" not in "\n".join(exc.value.tail)


_BARE_ERROR_WORKER = """
import sys
sys.stderr.write("@@ERROR@@not json at all\\n")
sys.stderr.flush()
sys.exit(1)
"""


def test_an_error_payload_that_is_not_json_is_still_reported(job, stems_dir, monkeypatch):
    """The worker json-encodes its message, but a crash handler somewhere else
    could write a bare string. Losing the reason would be worse than showing it
    raw."""
    monkeypatch.setattr(vs_mod, "_spawn_cmd", _stub(_BARE_ERROR_WORKER))

    with pytest.raises(SeparationError, match="not json at all"):
        vs_mod.split_vocals(job, stems_dir)


_SILENT_WORKER = """
import sys
sys.exit(1)
"""


def test_a_worker_that_dies_saying_nothing_still_produces_an_error(job, stems_dir, monkeypatch):
    monkeypatch.setattr(vs_mod, "_spawn_cmd", _stub(_SILENT_WORKER))

    with pytest.raises(SeparationError) as exc:
        vs_mod.split_vocals(job, stems_dir)

    assert "vocal split failed" in str(exc.value)


_HALF_WORKER = """
import sys, os
open(os.path.join(sys.argv[3], "lead_vocals.wav"), "wb").write(b"RIFF")
sys.stderr.write("@@DONE@@\\n")
sys.stderr.flush()
"""


def test_a_split_that_produced_only_one_file_is_a_failure(job, stems_dir, monkeypatch):
    """@@DONE@@ is the worker's claim; the files are the evidence. Trusting the
    claim would leave the UI showing a backing-vocals lane with no audio."""
    monkeypatch.setattr(vs_mod, "_spawn_cmd", _stub(_HALF_WORKER))

    with pytest.raises(SeparationError, match="backing_vocals.wav"):
        vs_mod.split_vocals(job, stems_dir)


def test_the_error_carries_the_device_it_ran_on(job, stems_dir, monkeypatch):
    """The quarantine record needs it: the same job often succeeds on cpu after
    failing on cuda, and that is the first thing to check."""
    monkeypatch.setattr(vs_mod, "get_demucs_device", lambda: "cuda")
    monkeypatch.setattr(vs_mod, "_spawn_cmd", _stub(_SILENT_WORKER))

    with pytest.raises(SeparationError) as exc:
        vs_mod.split_vocals(job, stems_dir)

    assert exc.value.device == "cuda"


_NOISY_SUCCESS_WORKER = """
import sys, os
sys.stderr.write("\\n")
sys.stderr.write("   \\n")
sys.stderr.write("loading model\\n")
out = sys.argv[3]
open(os.path.join(out, "lead_vocals.wav"), "wb").write(b"RIFF")
open(os.path.join(out, "backing_vocals.wav"), "wb").write(b"RIFF")
sys.stderr.write("@@DONE@@\\n")
sys.stderr.flush()
"""


def test_blank_progress_lines_do_not_confuse_the_reader(job, stems_dir, monkeypatch):
    monkeypatch.setattr(vs_mod, "_spawn_cmd", _stub(_NOISY_SUCCESS_WORKER))

    assert vs_mod.split_vocals(job, stems_dir) == ["lead_vocals", "backing_vocals"]


def test_the_process_registration_is_cleared_when_the_split_ends(job, stems_dir, monkeypatch):
    """A stale entry would let a later cancel signal an unrelated pid."""
    from app.core.registry import _procs

    monkeypatch.setattr(vs_mod, "_spawn_cmd", _stub(_NOISY_SUCCESS_WORKER))

    vs_mod.split_vocals(job, stems_dir)

    assert _procs.get(job.id) is None


# --------------------------------------------------------------------------
# warmup -- the setup wizard's model pre-download
# --------------------------------------------------------------------------


def _module(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def test_the_demucs_step_asks_for_the_model_the_pipeline_actually_uses(monkeypatch):
    """Warming a different checkpoint than the one separate.py loads would leave
    the first real job paying the download anyway."""
    from app.core.config import DEMUCS_MODEL
    from app.pipeline import warmup

    asked = []
    monkeypatch.setitem(sys.modules, "demucs", _module("demucs"))
    monkeypatch.setitem(
        sys.modules,
        "demucs.pretrained",
        _module("demucs.pretrained", get_model=lambda name: asked.append(name)),
    )

    warmup._warm_demucs()

    assert asked == [DEMUCS_MODEL]


def test_the_beat_model_is_warmed_through_the_healing_wrapper(monkeypatch):
    """beat_this collapses every failure into one ValueError, so a checkpoint
    truncated by a dropped download looks exactly like any other bad load and
    never heals on its own (#502)."""
    from app.core.config import BEAT_MODEL_CHECKPOINT
    from app.pipeline import warmup

    built = []
    seen = {}

    class _Audio2Beats:
        def __init__(self, **kw):
            built.append(kw)

    monkeypatch.setitem(sys.modules, "beat_this", _module("beat_this"))
    monkeypatch.setitem(
        sys.modules, "beat_this.inference", _module("beat_this.inference", Audio2Beats=_Audio2Beats)
    )

    def _spy(load, stale):
        seen["stale"] = list(stale())
        return load()

    monkeypatch.setattr("app.core.model_cache.load_or_heal", _spy)

    warmup._warm_beat_this()

    assert built and built[0]["checkpoint_path"] == BEAT_MODEL_CHECKPOINT
    # On the CPU: setup runs before anything else and must not contend for a GPU.
    assert built[0]["device"] == "cpu"
    assert any(str(p).endswith(f"beat_this-{BEAT_MODEL_CHECKPOINT}.ckpt") for p in seen["stale"])


def test_the_karaoke_model_is_warmed_through_the_healing_wrapper(monkeypatch):
    from app.core.config import VOCAL_SPLIT_MODEL
    from app.pipeline import warmup

    loaded = []
    seen = {}

    class _Separator:
        def __init__(self, **kw):
            self.kw = kw

        def load_model(self, model_filename):
            loaded.append(model_filename)

    monkeypatch.setitem(sys.modules, "audio_separator", _module("audio_separator"))
    monkeypatch.setitem(
        sys.modules,
        "audio_separator.separator",
        _module("audio_separator.separator", Separator=_Separator),
    )

    def _spy(load, stale):
        seen["stale"] = list(stale())
        return load()

    monkeypatch.setattr("app.core.model_cache.load_or_heal", _spy)

    warmup._warm_vocal_split()

    assert loaded == [VOCAL_SPLIT_MODEL]
    names = {p.name for p in seen["stale"]}
    # The indexes matter as much as the checkpoint: audio-separator reports a
    # truncated model as an unknown MD5, which is a lookup against them.
    assert VOCAL_SPLIT_MODEL in names
    assert "vr_model_data.json" in names
    assert "mdx_model_data.json" in names


def test_every_step_is_attempted_and_reported_in_order(monkeypatch, capsys):
    """The caller parses one line per model to drive the setup wizard's rows."""
    from app.pipeline import warmup

    monkeypatch.setattr(
        warmup,
        "_STEPS",
        (
            ("demucs", lambda: None),
            ("beat_this", lambda: None),
            ("sections", lambda: None),
            ("vocal_split", lambda: None),
        ),
    )

    assert warmup.main() == 0

    assert capsys.readouterr().out.splitlines() == [
        "WARMUP_OK demucs",
        "WARMUP_OK beat_this",
        "WARMUP_OK sections",
        "WARMUP_OK vocal_split",
    ]


def test_setup_still_succeeds_when_every_model_fails_to_download(monkeypatch, capsys):
    """A missing model degrades one feature. Failing setup over it would block
    an app that works fine offline for everything else."""
    from app.pipeline import warmup

    def _boom():
        raise RuntimeError("connection reset")

    monkeypatch.setattr(warmup, "_STEPS", (("demucs", _boom), ("beat_this", _boom)))

    assert warmup.main() == 0

    out = capsys.readouterr().out
    assert "WARMUP_FAILED demucs connection reset" in out
    assert "WARMUP_FAILED beat_this connection reset" in out
