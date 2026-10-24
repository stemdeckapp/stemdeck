"""The four model workers, without a GPU and without a model.

Each worker is a `python -m app.pipeline.<name>_worker` child process whose
model libraries are imported inside it. These tests run the real worker module,
as a real child process where the protocol is the point, with stand-in model
packages first on its PYTHONPATH: what is tested is the worker's own code (the
request loop, the @@DONE@@ / @@ERROR@@ lines, the exit codes, the one JSON line
on stdout), not demucs, audio-separator, All-In-One or Whisper.

Coverage of the child processes needs `scripts/coverage_run.py`; plain
`pytest --cov` traces only this process.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import textwrap
import threading
import types
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.core.config import DEMUCS_MODEL
from app.pipeline import section_worker, transcribe_worker

ROOT = Path(__file__).resolve().parents[1]


def _write_pkg(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
    return root


def _run_worker(
    module: str,
    args: list[str],
    fakes: Path,
    tmp_path: Path,
    stdin: str = "",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "STEMDECK_PARENT_PID"}
    env.pop("CUDA_VISIBLE_DEVICES", None)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(fakes), env.get("PYTHONPATH", "")) if p)
    # The child reads its folders from the environment: keep it inside tmp_path.
    for name in ("JOBS", "DATA", "CACHE"):
        env[f"STEMDECK_{name}_DIR"] = str(tmp_path / f"child-{name.lower()}")
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", f"app.pipeline.{module}", *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=120,
    )


# --- demucs_worker -------------------------------------------------------------

_FAKE_DEMUCS = {
    "demucs/__init__.py": "",
    "demucs/pretrained.py": """
        class _Model:
            audio_channels = 2
            samplerate = 8000
            sources = ["vocals", "drums"]

            def eval(self):
                return self

            def cpu(self):
                return self


        def get_model(name):
            return _Model()
    """,
    "demucs/separate.py": """
        import numpy as np


        def load_track(path, channels, rate):
            text = open(path).read().strip()
            if text == "fail":
                raise RuntimeError("cannot decode this source")
            level = float(text)
            n = 64
            return np.stack([np.full(n, level), np.linspace(-1.0, 1.0, n) * level])
    """,
    "demucs/apply.py": """
        import sys

        import numpy as np

        LAST = {}


        def apply_model(model, mix, device, shifts, split, overlap, progress, num_workers,
                        segment):
            sys.stderr.write(" 50%|#####     |\\r100%|##########|\\n")
            LAST.update(device=device, shifts=shifts)
            # Source i is the (normalised) mix times i + 1.
            return np.stack([mix * (i + 1) for i in range(len(model.sources))], axis=1)
    """,
    "demucs/audio.py": """
        import json

        from demucs import apply


        def save_audio(tensor, path, samplerate, clip, bits_per_sample, as_float):
            with open(path, "w") as f:
                json.dump({"first": float(tensor[0][0]), "rate": samplerate, **apply.LAST}, f)
    """,
}


def _demucs_job(tmp_path: Path, name: str, content: str, **extra) -> tuple[str, Path]:
    src = tmp_path / f"{name}.src"
    src.write_text(content)
    job_dir = tmp_path / f"job-{name}"
    job_dir.mkdir()
    return json.dumps({"source": str(src), "job_dir": str(job_dir), **extra}), job_dir


def _stem(job_dir: Path, name: str, stem: str) -> dict:
    return json.loads((job_dir / DEMUCS_MODEL / name / f"{stem}.wav").read_text())


def test_demucs_worker_serves_jobs_until_stdin_closes(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    first, dir1 = _demucs_job(tmp_path, "one", "0.5", shifts=2)
    second, dir2 = _demucs_job(tmp_path, "two", "0.25")

    r = _run_worker("demucs_worker", ["mps"], fakes, tmp_path, f"{first}\n\n{second}\n")

    assert r.returncode == 0, r.stderr
    assert r.stderr.count("@@DONE@@") == 2
    assert "@@ERROR@@" not in r.stderr
    assert "50%" in r.stderr  # tqdm-style progress reaches the parent's reader
    one = _stem(dir1, "one", "vocals")
    # Normalised for the model, then put back: the first stem is the input.
    assert one["first"] == pytest.approx(0.5)
    assert one["rate"] == 8000
    assert one["shifts"] == 2
    assert one["device"] == "mps"
    # Source 2 is twice the normalised mix: 2 * (x - mean) + mean.
    assert _stem(dir1, "one", "drums")["first"] == pytest.approx(0.75)
    two = _stem(dir2, "two", "vocals")
    assert two["first"] == pytest.approx(0.25)
    assert two["shifts"] == 1  # default when the request leaves it out


def test_demucs_worker_defaults_to_the_cpu(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    job, job_dir = _demucs_job(tmp_path, "one", "0.5")

    r = _run_worker("demucs_worker", [], fakes, tmp_path, job + "\n")

    assert r.returncode == 0, r.stderr
    assert _stem(job_dir, "one", "vocals")["device"] == "cpu"


def test_demucs_worker_exits_on_the_first_failed_job(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    ok, _ = _demucs_job(tmp_path, "ok", "0.5")
    bad, _ = _demucs_job(tmp_path, "bad", "fail")
    never, never_dir = _demucs_job(tmp_path, "never", "0.5")

    r = _run_worker("demucs_worker", ["cpu"], fakes, tmp_path, f"{ok}\n{bad}\n{never}\n")

    assert r.returncode == 1
    lines = [ln for ln in r.stderr.splitlines() if ln.startswith("@@")]
    assert lines == ["@@DONE@@", '@@ERROR@@"cannot decode this source"']
    # A worker in an unknown state never serves another job.
    assert not (never_dir / DEMUCS_MODEL).exists()


def test_demucs_worker_reports_a_request_that_is_not_json(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)

    r = _run_worker("demucs_worker", ["cpu"], fakes, tmp_path, "{not json\n")

    assert r.returncode == 1
    assert "@@ERROR@@" in r.stderr
    assert "@@DONE@@" not in r.stderr


# --- vocal_split_worker -----------------------------------------------------------

_FAKE_SEPARATOR = {
    "audio_separator/__init__.py": "",
    "audio_separator/separator.py": """
        import json
        import os


        class Separator:
            def __init__(self, log_level, model_file_dir, output_dir, output_format):
                self.out = output_dir
                self.model = None

            def load_model(self, model_filename):
                self.model = model_filename

            def separate(self, path, names):
                if os.environ.get("FAKE_FAIL") == "separate":
                    raise RuntimeError("inference failed")
                for name in names.values():
                    with open(os.path.join(self.out, name + ".wav"), "w") as f:
                        json.dump(
                            {
                                "input": path,
                                "model": self.model,
                                "cuda": os.environ.get("CUDA_VISIBLE_DEVICES"),
                            },
                            f,
                        )
    """,
}


@pytest.mark.parametrize(("device", "cuda_env"), [("cpu", ""), ("cuda", None)])
def test_vocal_split_worker_writes_lead_and_backing(tmp_path, device, cuda_env):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_SEPARATOR)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    vocals = tmp_path / "vocals.wav"
    vocals.write_bytes(b"")

    r = _run_worker("vocal_split_worker", [device, str(vocals), str(out_dir)], fakes, tmp_path)

    assert r.returncode == 0, r.stderr
    assert r.stderr.strip().splitlines()[-1] == "@@DONE@@"
    lead = json.loads((out_dir / "lead_vocals.wav").read_text())
    backing = json.loads((out_dir / "backing_vocals.wav").read_text())
    assert lead["input"] == str(vocals)
    assert lead["model"] and lead["model"] == backing["model"]
    # "cpu" hides the GPU from onnxruntime; any other device leaves it visible.
    assert lead["cuda"] == cuda_env


def test_vocal_split_worker_without_arguments_prints_its_usage(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_SEPARATOR)

    r = _run_worker("vocal_split_worker", ["cpu"], fakes, tmp_path)

    assert r.returncode == 1
    assert r.stderr.startswith("@@ERROR@@usage:")


def test_vocal_split_worker_reports_a_failed_separation(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_SEPARATOR)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    r = _run_worker(
        "vocal_split_worker",
        ["cpu", str(tmp_path / "vocals.wav"), str(out_dir)],
        fakes,
        tmp_path,
        extra_env={"FAKE_FAIL": "separate"},
    )

    assert r.returncode == 1
    last = r.stderr.strip().splitlines()[-1]
    assert last.startswith("@@ERROR@@")
    # The worker names the step that failed as a code (#832).
    assert json.loads(last[len("@@ERROR@@") :]) == {
        "message": "inference failed",
        "code": "worker_failed",
    }
    assert "@@DONE@@" not in r.stderr


# --- section_worker ---------------------------------------------------------------

_FAKE_ALLIN1 = {
    "torch/__init__.py": """
        import contextlib

        no_grad = contextlib.nullcontext


        class Tensor:  # scipy probes torch.Tensor once torch is importable
            pass
    """,
    "allin1_infer/__init__.py": "",
    "allin1_infer/config.py": 'HARMONIX_LABELS = ["start", "end", "intro", "verse", "chorus"]\n',
    "allin1_infer/spectrogram.py": """
        def extract_spectrograms(paths, out_dir, multiprocess):
            return [out_dir / "song.npy"]
    """,
    "allin1_infer/models.py": """
        def load_pretrained_model(model_name, device):
            print("loading " + model_name)  # third-party chatter on stdout
            return object()
    """,
    "allin1_infer/helpers.py": """
        import os
        from types import SimpleNamespace


        def _seg(start, end, label):
            return SimpleNamespace(start=start, end=end, label=label)


        def run_inference(path, spec_path, model, device, include_activations,
                          include_embeddings):
            print("inference chatter")
            result = SimpleNamespace(
                segments=[_seg(0, 12.5, "intro"), _seg(12.5, 40, "verse")],
                activations=None,
                embeddings=None,
                activation_fps=None,
            )
            if os.environ.get("FAKE_RESULT") == "two":
                return [result, result]
            return [result]
    """,
}


def _section_stems(tmp_path: Path, names=("bass", "drums", "other", "vocals")) -> Path:
    stems = tmp_path / "stems"
    stems.mkdir()
    for name in names:
        (stems / f"{name}.wav").write_bytes(b"")
    return stems


def _section_args(stems: Path) -> list[str]:
    return ["--stems-dir", str(stems), "--identifier", "song", "--model", "harmonix-all"]


def test_section_worker_prints_exactly_one_json_line(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_ALLIN1)
    stems = _section_stems(tmp_path)

    r = _run_worker("section_worker", _section_args(stems), fakes, tmp_path)

    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert len(lines) == 1, r.stdout
    segments = json.loads(lines[0])["segments"]
    assert [s["start"] for s in segments] == [0.0, 12.5]
    assert segments[-1]["end"] == 40.0
    # The model's own prints went to stderr, where they cannot be parsed as data.
    assert "inference chatter" in r.stderr
    assert "loading harmonix-all" in r.stderr


def test_section_worker_fails_on_an_unexpected_result_count(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_ALLIN1)
    stems = _section_stems(tmp_path)

    r = _run_worker(
        "section_worker", _section_args(stems), fakes, tmp_path, extra_env={"FAKE_RESULT": "two"}
    )

    assert r.returncode != 0
    assert r.stdout == ""
    assert "unexpected result count" in r.stderr


def test_section_worker_needs_every_stem_it_reads(tmp_path, monkeypatch):
    monkeypatch.delenv("STEMDECK_PARENT_PID", raising=False)
    stems = _section_stems(tmp_path, names=("bass", "drums", "vocals"))  # no "other"

    with pytest.raises(FileNotFoundError):
        section_worker.main(_section_args(stems))


def test_section_result_segments_unwraps_and_validates():
    seg = types.SimpleNamespace(start=1, end=2, label="verse")
    one = types.SimpleNamespace(segments=[seg])

    assert section_worker._result_segments([one]) == [{"start": 1.0, "end": 2.0, "label": "verse"}]
    assert section_worker._result_segments(one) == [{"start": 1.0, "end": 2.0, "label": "verse"}]
    with pytest.raises(RuntimeError, match="unexpected result count"):
        section_worker._result_segments([one, one])
    with pytest.raises(RuntimeError, match="no segments"):
        section_worker._result_segments(types.SimpleNamespace(segments=None))


def test_section_beat_grid_is_optional_and_never_fatal(tmp_path, capsys):
    good = tmp_path / "beats.json"
    good.write_text(json.dumps({"beats": [0.5, 1.0]}), encoding="utf-8")
    bad = tmp_path / "broken.json"
    bad.write_text("{broken", encoding="utf-8")

    assert section_worker._load_beat_grid(None) is None
    assert section_worker._load_beat_grid(tmp_path / "missing.json") is None
    assert section_worker._load_beat_grid(good) == {"beats": [0.5, 1.0]}
    assert section_worker._load_beat_grid(bad) is None
    assert "unreadable beat grid" in capsys.readouterr().err


def test_section_heartbeat_ticks_on_stderr_until_stopped(monkeypatch, capsys):
    monkeypatch.setattr(section_worker, "_HEARTBEAT_SECONDS", 0.01)
    stop = threading.Event()
    t = threading.Thread(target=section_worker._heartbeat, args=(stop,))
    t.start()
    threading.Event().wait(0.1)
    stop.set()
    t.join(2)

    assert not t.is_alive()
    assert capsys.readouterr().err.count("SECTION_HEARTBEAT") >= 2


def _in_process_allin1(monkeypatch, segments) -> None:
    result = types.SimpleNamespace(
        segments=segments, activations=None, embeddings=None, activation_fps=None
    )
    mods = {
        "torch": types.SimpleNamespace(no_grad=contextlib.nullcontext),
        "allin1_infer": types.ModuleType("allin1_infer"),
        "allin1_infer.config": types.SimpleNamespace(HARMONIX_LABELS=["verse"]),
        "allin1_infer.helpers": types.SimpleNamespace(run_inference=lambda **kw: result),
        "allin1_infer.models": types.SimpleNamespace(load_pretrained_model=lambda **kw: object()),
        "allin1_infer.spectrogram": types.SimpleNamespace(
            extract_spectrograms=lambda paths, out, multiprocess: [out / "x.npy"]
        ),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delenv("STEMDECK_PARENT_PID", raising=False)


def test_section_refinement_failure_falls_back_to_the_model_segments(tmp_path, monkeypatch, capsys):
    seg = types.SimpleNamespace(start=0, end=30, label="verse")
    _in_process_allin1(monkeypatch, [seg])

    def boom(*_a):
        raise ValueError("bad evidence")

    monkeypatch.setattr(section_worker, "refine_segments", boom)

    assert section_worker.main(_section_args(_section_stems(tmp_path))) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"segments": [{"start": 0.0, "end": 30.0, "label": "verse"}]}
    assert "fell back after ValueError" in err


def test_section_refinement_result_is_what_is_printed(tmp_path, monkeypatch, capsys):
    seg = types.SimpleNamespace(start=0, end=30, label="verse")
    _in_process_allin1(monkeypatch, [seg])
    refined = [{"start": 0.0, "end": 29.5, "label": "chorus"}]
    monkeypatch.setattr(section_worker, "refine_segments", lambda *_a: refined)

    assert section_worker.main(_section_args(_section_stems(tmp_path))) == 0
    assert json.loads(capsys.readouterr().out) == {"segments": refined}


# --- transcribe_worker -------------------------------------------------------------


class _Whisper:
    """A stand-in whisper module and model; `calls` records what the worker asked."""

    def __init__(self, segments, *, multilingual=True, probs=None):
        self.calls: list[tuple] = []
        self.segments = segments
        self.multilingual = multilingual
        self.probs = probs or {"en": 0.9, "pt": 0.1}

    def module(self) -> types.ModuleType:
        outer = self

        class Mel:
            def to(self, device):
                return self

            def half(self):
                outer.calls.append(("mel.half",))
                return self

        class Model:
            is_multilingual = outer.multilingual
            dims = types.SimpleNamespace(n_mels=80)
            device = "cpu"

            def detect_language(self, mel):
                outer.calls.append(("detect",))
                return None, outer.probs

            def transcribe(self, audio, **kw):
                outer.calls.append(("transcribe", len(audio), kw["language"], kw["fp16"]))
                return {"segments": outer.segments}

        mod = types.ModuleType("whisper")
        mod._MODELS = {"small": "https://example.invalid/models/small.pt"}
        mod.log_mel_spectrogram = lambda audio, n_mels: Mel()
        mod.pad_or_trim = lambda audio: audio
        mod.Model = Model
        return mod


def _install_whisper(monkeypatch, fake: _Whisper, *, cuda_free_mb=None, seconds=4.0):
    whisper = fake.module()
    cuda = types.SimpleNamespace(
        is_available=lambda: cuda_free_mb is not None,
        mem_get_info=lambda: ((cuda_free_mb or 0) * 1024 * 1024, 0),
        max_memory_allocated=lambda: 700 * 1024 * 1024,
    )
    torch = types.SimpleNamespace(cuda=cuda, nn=types.SimpleNamespace(LayerNorm=type(None)))
    monkeypatch.setitem(sys.modules, "whisper", whisper)
    monkeypatch.setitem(sys.modules, "torch", torch)
    n = int(seconds * transcribe_worker._SAMPLE_RATE)
    monkeypatch.setattr(
        transcribe_worker, "load_audio", lambda path: np.full(n, 0.1, dtype=np.float32)
    )
    loaded: list[tuple[str, str]] = []

    def load_model(_whisper, _torch, name, device, root):
        loaded.append((name, device))
        return whisper.Model()

    monkeypatch.setattr(transcribe_worker, "load_model", load_model)
    return loaded


def _targs(tmp_path: Path, *extra: str, device: str = "cpu"):
    return transcribe_worker._parser().parse_args(
        [
            "--audio", str(tmp_path / "vocals.wav"),
            "--device", device,
            "--model", "turbo",
            "--fallback-model", "small",
            "--download-root", str(tmp_path),
            "--min-free-mb", "3072",
            "--small-min-free-mb", "1536",
            "--silence-sec", "2",
            *extra,
        ]
    )  # fmt: skip


_SEG = {
    "start": 0.5,
    "end": 1.5,
    "text": " la la",
    "words": [{"start": 0.5, "end": 1.0, "word": " la", "probability": 0.8}, {
        "start": 1.0, "end": 1.5, "word": " la"}],
    "no_speech_prob": 0.01,
}  # fmt: skip


def test_transcribe_maps_segments_and_words(tmp_path, monkeypatch, capsys):
    fake = _Whisper([_SEG])
    _install_whisper(monkeypatch, fake)

    out = transcribe_worker.transcribe(_targs(tmp_path))

    assert out["language"] == "en" and out["language_probability"] == 0.9
    assert out["model"] == "small" and out["device"] == "cpu"
    assert out["peak_vram_mb"] is None
    [seg] = out["segments"]
    assert seg["text"] == " la la" and seg["start"] == 0.5 and seg["end"] == 1.5
    assert seg["words"][1] == {"start": 1.0, "end": 1.5, "word": " la", "probability": 0.0}
    assert seg["avg_logprob"] == 0.0 and seg["no_speech_prob"] == 0.01
    # The model file is not in the download root: the parent is told it downloads.
    assert "@@PHASE@@download" in capsys.readouterr().err
    assert ("transcribe", 4 * 16000, "en", False) in fake.calls


def test_transcribe_with_a_cached_model_reports_loading(tmp_path, monkeypatch, capsys):
    _install_whisper(monkeypatch, _Whisper([]))
    (tmp_path / "small.pt").write_bytes(b"")

    transcribe_worker.transcribe(_targs(tmp_path))

    err = capsys.readouterr().err
    assert "@@PHASE@@load" in err and "@@PHASE@@download" not in err


def test_transcribe_clips_are_heard_alone_and_keep_the_stem_times(tmp_path, monkeypatch):
    fake = _Whisper([_SEG])
    _install_whisper(monkeypatch, fake, seconds=10.0)

    # The second clip is under half a second and is not worth hearing.
    out = transcribe_worker.transcribe(_targs(tmp_path, "--clips", "2:4,6:6.2"))

    heard = [c for c in fake.calls if c[0] == "transcribe"]
    assert heard == [("transcribe", 2 * 16000, "en", False)]
    [seg] = out["segments"]
    assert seg["start"] == 2.5 and seg["words"][0]["start"] == 2.5


def test_transcribe_known_language_skips_detection(tmp_path, monkeypatch):
    fake = _Whisper([])
    _install_whisper(monkeypatch, fake)

    out = transcribe_worker.transcribe(_targs(tmp_path, "--language", "pt"))

    assert out["language"] == "pt" and out["language_probability"] == 1.0
    assert ("detect",) not in fake.calls


def test_transcribe_english_only_model_never_detects(tmp_path, monkeypatch):
    fake = _Whisper([], multilingual=False)
    _install_whisper(monkeypatch, fake)

    out = transcribe_worker.transcribe(_targs(tmp_path, "--language", "pt"))

    assert out["language"] == "en"
    assert ("detect",) not in fake.calls


def test_transcribe_on_a_gpu_with_room_uses_the_big_model_in_fp16(tmp_path, monkeypatch):
    fake = _Whisper([_SEG])
    loaded = _install_whisper(monkeypatch, fake, cuda_free_mb=8000)

    out = transcribe_worker.transcribe(_targs(tmp_path, device="cuda"))

    assert loaded == [("turbo", "cuda")]
    assert out["device"] == "cuda" and out["peak_vram_mb"] == 700
    assert ("mel.half",) in fake.calls
    assert any(c[0] == "transcribe" and c[3] is True for c in fake.calls)


def test_transcribe_on_a_full_gpu_falls_back_to_the_cpu(tmp_path, monkeypatch, capsys):
    loaded = _install_whisper(monkeypatch, _Whisper([]), cuda_free_mb=500)

    out = transcribe_worker.transcribe(_targs(tmp_path, device="cuda"))

    assert loaded == [("small", "cpu")]
    assert out["device"] == "cpu" and out["peak_vram_mb"] is None
    assert "not enough free VRAM (500 MB)" in capsys.readouterr().err


def test_free_vram_is_unknown_when_cuda_cannot_answer():
    def broken():
        raise RuntimeError("no driver")

    torch = types.SimpleNamespace(cuda=types.SimpleNamespace(mem_get_info=broken))
    assert transcribe_worker._free_vram_mb(torch) is None
    ok = types.SimpleNamespace(
        cuda=types.SimpleNamespace(mem_get_info=lambda: (3 * 1024 * 1024 + 5, 0))
    )
    assert transcribe_worker._free_vram_mb(ok) == 3


def test_load_model_halves_the_weights_on_a_gpu_but_not_layer_norm():
    events: list[str] = []

    class LayerNorm:
        def float(self):
            events.append("layernorm.float")

    class Linear:
        def float(self):
            events.append("linear.float")

    class Model:
        def half(self):
            events.append("half")

        def modules(self):
            return [LayerNorm(), Linear()]

        def to(self, device):
            events.append(f"to:{device}")
            return self

    whisper = types.SimpleNamespace(load_model=lambda name, device, download_root: Model())
    torch = types.SimpleNamespace(nn=types.SimpleNamespace(LayerNorm=LayerNorm))

    transcribe_worker.load_model(whisper, torch, "turbo", "cuda", Path("."))
    assert events == ["half", "layernorm.float", "to:cuda"]

    events.clear()
    transcribe_worker.load_model(whisper, torch, "small", "cpu", Path("."))
    assert events == []


def test_transcribe_main_prints_one_json_line(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STEMDECK_PARENT_PID", raising=False)
    (tmp_path / "vocals.wav").write_bytes(b"")
    monkeypatch.setattr(
        transcribe_worker, "transcribe", lambda args: print("chatter") or {"language": "pt"}
    )
    argv = [
        "--audio", str(tmp_path / "vocals.wav"), "--device", "cpu", "--model", "t",
        "--fallback-model", "s", "--download-root", str(tmp_path), "--min-free-mb", "1",
        "--small-min-free-mb", "1", "--silence-sec", "1",
    ]  # fmt: skip

    assert transcribe_worker.main(argv) == 0
    out, err = capsys.readouterr()
    assert out == '{"language":"pt"}\n'
    assert "chatter" in err


def test_transcribe_main_refuses_a_missing_stem(tmp_path, monkeypatch):
    monkeypatch.delenv("STEMDECK_PARENT_PID", raising=False)
    args = _targs(tmp_path)
    argv = [
        "--audio", str(args.audio), "--device", "cpu", "--model", "t",
        "--fallback-model", "s", "--download-root", str(tmp_path), "--min-free-mb", "1",
        "--small-min-free-mb", "1", "--silence-sec", "1",
    ]  # fmt: skip
    with pytest.raises(FileNotFoundError):
        transcribe_worker.main(argv)


_FAKE_WHISPER_PKG = {
    "torch/__init__.py": """
        from types import SimpleNamespace


        class Tensor:  # scipy probes torch.Tensor once torch is importable
            pass


        cuda = SimpleNamespace(is_available=lambda: False)
        nn = SimpleNamespace(LayerNorm=type(None))
    """,
    "whisper/__init__.py": """
        from types import SimpleNamespace

        _MODELS = {}


        class _Mel:
            def to(self, device):
                return self


        class _Model:
            is_multilingual = True
            dims = SimpleNamespace(n_mels=80)
            device = "cpu"

            def detect_language(self, mel):
                return None, {"pt": 0.97, "en": 0.03}

            def transcribe(self, audio, **kw):
                print("decoding chatter")
                return {"segments": [{"start": 0.0, "end": 1.0, "text": " olá",
                                      "words": [{"start": 0.1, "end": 0.9, "word": " olá",
                                                 "probability": 0.9}]}]}


        def load_model(name, device, download_root):
            return _Model()


        def log_mel_spectrogram(audio, n_mels):
            return _Mel()


        def pad_or_trim(audio):
            return audio
    """,
}


def test_transcribe_worker_process_prints_one_utf8_json_line(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_WHISPER_PKG)
    vocals = tmp_path / "vocals.wav"
    sf.write(str(vocals), np.zeros((44100, 2), dtype=np.float32), 44100)
    argv = [
        "--audio", str(vocals), "--device", "cpu", "--model", "turbo",
        "--fallback-model", "small", "--download-root", str(tmp_path / "models"),
        "--min-free-mb", "3072", "--small-min-free-mb", "1536", "--silence-sec", "2",
    ]  # fmt: skip

    r = subprocess.run(
        [sys.executable, "-m", "app.pipeline.transcribe_worker", *argv],
        capture_output=True,
        cwd=ROOT,
        env={
            **{k: v for k, v in os.environ.items() if k != "STEMDECK_PARENT_PID"},
            "PYTHONPATH": os.pathsep.join(
                p for p in (str(fakes), os.environ.get("PYTHONPATH", "")) if p
            ),
            # What transcribe.py sets for the worker it spawns.
            "PYTHONIOENCODING": "utf-8:replace",
        },
        timeout=120,
    )

    assert r.returncode == 0, r.stderr.decode(errors="replace")
    lines = r.stdout.splitlines()
    assert len(lines) == 1
    out = json.loads(lines[0].decode("utf-8"))
    assert out["language"] == "pt" and out["model"] == "small"
    assert out["segments"][0]["words"][0]["word"] == " olá"
    # Written as UTF-8 text, not \u escapes.
    assert " olá".encode() in r.stdout
    assert b"decoding chatter" in r.stderr
    assert b"@@PHASE@@transcribe" in r.stderr
