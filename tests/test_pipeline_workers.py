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
import socket
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
    # And torch.hub's cache: the worker checks the checkpoints it finds there
    # against their pins, and removes one that does not match.
    env["TORCH_HOME"] = str(tmp_path / "child-torch")
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
        import os


        class _Model:
            audio_channels = 2
            samplerate = 8000

            def __init__(self, name):
                self.name = name
                if name == "htdemucs_ft":
                    self.sources = ["drums", "bass", "other", "vocals"]
                    # Source i of this model is the normalised mix times i + 10.
                    self.factor = 10
                    # A bag of four, like the real one: four progress sweeps.
                    self.models = [None] * 4
                else:
                    self.sources = os.environ.get("FAKE_SOURCES", "vocals,drums").split(",")
                    self.factor = 1

            def eval(self):
                return self

            def cpu(self):
                return self


        def get_model(name):
            return _Model(name)
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
            # Source i is the (normalised) mix times i + the model's factor.
            return np.stack(
                [mix * (i + model.factor) for i in range(len(model.sources))], axis=1
            )
    """,
    "demucs/audio.py": """
        import json

        import numpy as np

        from demucs import apply


        def save_audio(tensor, path, samplerate, clip, bits_per_sample, as_float):
            # demucs.audio.prevent_clip's two modes, so a clamp that cuts a
            # sample shows in what is written.
            data = np.asarray(tensor, dtype=float)
            if clip == "clamp":
                data = np.clip(data, -0.99, 0.99)
            elif clip == "rescale":
                data = data / max(1.01 * np.abs(data).max(), 1)
            with open(path, "w") as f:
                json.dump(
                    {
                        "first": float(data[0][0]),
                        "data": data.tolist(),
                        "clip": clip,
                        "rate": samplerate,
                        **apply.LAST,
                    },
                    f,
                )
    """,
}

_SIX = ("drums", "bass", "other", "vocals", "guitar", "piano")


def _demucs_job(tmp_path: Path, name: str, content: str, **extra) -> tuple[str, Path]:
    src = tmp_path / f"{name}.src"
    src.write_text(content)
    job_dir = tmp_path / f"job-{name}"
    job_dir.mkdir()
    return json.dumps({"source": str(src), "job_dir": str(job_dir), **extra}), job_dir


def _stem(job_dir: Path, name: str, stem: str) -> dict:
    return json.loads((job_dir / DEMUCS_MODEL / name / f"{stem}.wav").read_text())


def _fake_mix(level: float) -> np.ndarray:
    """What the fake load_track reads for a source holding ``level``."""
    n = 64
    return np.stack([np.full(n, level), np.linspace(-1.0, 1.0, n) * level])


def _fake_sources(mix: np.ndarray, count: int, factor: int = 1) -> list[np.ndarray]:
    """What the fake Demucs makes of ``mix``: source i is the normalised mix
    times i + factor, put back to the mix's level the way the worker does."""
    ref = mix.mean(0)
    norm = (mix - ref.mean()) / ref.std()
    return [norm * (i + factor) * ref.std() + ref.mean() for i in range(count)]


def _scale(stems: dict[str, np.ndarray]) -> float:
    """The shared scale, from the clamp's own ceiling (stem_plan)."""
    from app.pipeline.stem_plan import CLAMP_CEILING, CLAMP_MARGIN

    peak = max(float(np.abs(a).max()) for a in stems.values())
    return max(1.0, peak * (1 + CLAMP_MARGIN) / CLAMP_CEILING)


def _phases(stderr: str) -> list[str]:
    return [ln for ln in stderr.splitlines() if ln.startswith("@@PHASE@@")]


def test_demucs_worker_serves_jobs_until_stdin_closes(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    first, dir1 = _demucs_job(tmp_path, "one", "0.5", shifts=2)
    second, dir2 = _demucs_job(tmp_path, "two", "0.25")

    r = _run_worker("demucs_worker", ["mps"], fakes, tmp_path, f"{first}\n\n{second}\n")

    assert r.returncode == 0, r.stderr
    assert r.stderr.count("@@DONE@@") == 2
    assert "@@ERROR@@" not in r.stderr
    assert "50%" in r.stderr  # tqdm-style progress reaches the parent's reader
    # A request without a plan is the base model alone: one pass.
    assert _phases(r.stderr) == [f"@@PHASE@@{DEMUCS_MODEL}"] * 2
    # Normalised for the model, then put back, then one scale for all stems:
    # the first stem is the input, the second twice the normalised mix.
    vocals, drums = _fake_sources(_fake_mix(0.5), 2)
    stems = {"vocals": vocals, "drums": drums, "other": _fake_mix(0.5) - vocals - drums}
    s = _scale(stems)
    one = _stem(dir1, "one", "vocals")
    assert one["first"] == pytest.approx(0.5 / s)
    assert _stem(dir1, "one", "drums")["first"] == pytest.approx(0.75 / s)
    assert one["rate"] == 8000
    assert one["shifts"] == 2
    assert one["device"] == "mps"
    two = _stem(dir2, "two", "vocals")
    assert two["shifts"] == 1  # default when the request leaves it out


def test_demucs_worker_defaults_to_the_cpu(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    job, job_dir = _demucs_job(tmp_path, "one", "0.5")

    r = _run_worker("demucs_worker", [], fakes, tmp_path, job + "\n")

    assert r.returncode == 0, r.stderr
    assert _stem(job_dir, "one", "vocals")["device"] == "cpu"


def test_demucs_worker_stems_add_up_to_the_mix_with_one_shared_scale(tmp_path):
    """ "other" is the mix minus the five kept stems, not Demucs' own "other",
    and every stem is written divided by the same s with clip "clamp", so the
    six files still add up to the song. The old worker wrote Demucs' "other"
    and rescaled each stem on its own."""
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    job, job_dir = _demucs_job(tmp_path, "one", "0.5")

    r = _run_worker(
        "demucs_worker", ["cpu"], fakes, tmp_path, job + "\n", {"FAKE_SOURCES": ",".join(_SIX)}
    )

    assert r.returncode == 0, r.stderr
    mix = _fake_mix(0.5)
    by_name = dict(zip(_SIX, _fake_sources(mix, 6), strict=True))
    kept = {n: by_name[n] for n in ("drums", "bass", "vocals", "guitar", "piano")}
    expected = {**kept, "other": mix - sum(kept.values())}
    s = _scale(expected)
    assert s > 1  # the fake's stems are loud: the scale is applied and said
    assert f"@@SCALE@@{s:.4f}" in r.stderr
    written = {n: np.array(_stem(job_dir, "one", n)["data"]) for n in expected}
    for name, audio in expected.items():
        np.testing.assert_allclose(written[name], audio / s, atol=1e-9)
        assert _stem(job_dir, "one", name)["clip"] == "clamp"
    # Demucs' own "other" (source 2) is not what was written.
    assert not np.allclose(written["other"], by_name["other"] / s)
    np.testing.assert_allclose(sum(written.values()), mix / s, atol=1e-9)


def test_demucs_worker_takes_each_stem_from_the_model_the_plan_names(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    plan = {
        "vocals": "htdemucs_6s",
        "drums": "htdemucs_ft",
        "bass": "htdemucs_ft",
        "guitar": "htdemucs_6s",
        "piano": "htdemucs_6s",
    }
    job, job_dir = _demucs_job(tmp_path, "one", "0.5", plan=plan)

    r = _run_worker(
        "demucs_worker",
        ["cpu"],
        fakes,
        tmp_path,
        job + "\n",
        {"FAKE_SOURCES": ",".join(_SIX), "STEMDECK_DEMUCS_MODEL": "htdemucs_6s"},
    )

    assert r.returncode == 0, r.stderr
    # One pass per model, the base first, both on the mix (no vocal model ran),
    # each saying how many progress sweeps it makes.
    assert _phases(r.stderr) == ["@@PHASE@@htdemucs_6s", "@@PHASE@@htdemucs_ft"]
    sweeps = [ln for ln in r.stderr.splitlines() if ln.startswith("@@SWEEPS@@")]
    assert sweeps == ["@@SWEEPS@@1", "@@SWEEPS@@4"]
    mix = _fake_mix(0.5)
    six = dict(zip(_SIX, _fake_sources(mix, 6), strict=True))
    ft = dict(zip(("drums", "bass", "other", "vocals"), _fake_sources(mix, 4, 10), strict=True))
    kept = {
        "vocals": six["vocals"],
        "drums": ft["drums"],
        "bass": ft["bass"],
        "guitar": six["guitar"],
        "piano": six["piano"],
    }
    expected = {**kept, "other": mix - sum(kept.values())}
    s = _scale(expected)
    root = job_dir / "htdemucs_6s" / "one"
    for name, audio in expected.items():
        data = np.array(json.loads((root / f"{name}.wav").read_text())["data"])
        np.testing.assert_allclose(data, audio / s, atol=1e-9)


def test_best_quality_doubles_the_sweeps(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    job, _ = _demucs_job(tmp_path, "one", "0.5", shifts=2)
    r = _run_worker("demucs_worker", ["cpu"], fakes, tmp_path, job + "\n")
    assert r.returncode == 0, r.stderr
    assert "@@SWEEPS@@2" in r.stderr.splitlines()


def test_a_silent_source_gives_silent_stems_not_an_error(tmp_path):
    """Normalising silence divides by zero: every sample would be NaN, and
    the job would fail on the GPU and again on the CPU."""
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    job, job_dir = _demucs_job(tmp_path, "one", "0")

    r = _run_worker(
        "demucs_worker", ["cpu"], fakes, tmp_path, job + "\n", {"FAKE_SOURCES": ",".join(_SIX)}
    )

    assert r.returncode == 0, r.stderr
    assert "@@DONE@@" in r.stderr and "@@SCALE@@" not in r.stderr
    for name in ("vocals", "drums", "bass", "guitar", "piano", "other"):
        assert not np.array(_stem(job_dir, "one", name)["data"]).any()


def test_demucs_worker_refuses_a_plan_it_cannot_run(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    plan = {
        "vocals": "a_model_from_the_future",
        "drums": "htdemucs_6s",
        "bass": "htdemucs_6s",
        "guitar": "htdemucs_6s",
        "piano": "htdemucs_6s",
    }
    job, job_dir = _demucs_job(tmp_path, "one", "0.5", plan=plan)

    r = _run_worker("demucs_worker", ["cpu"], fakes, tmp_path, job + "\n")

    assert r.returncode == 1
    lines = [ln for ln in r.stderr.splitlines() if ln.startswith("@@")]
    assert lines == [
        "@@READY@@",
        '@@ERROR@@"the separation plan has no usable model for vocals"',
    ]
    assert not (job_dir / DEMUCS_MODEL).exists()


def test_demucs_worker_exits_on_the_first_failed_job(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    ok, _ = _demucs_job(tmp_path, "ok", "0.5")
    bad, _ = _demucs_job(tmp_path, "bad", "fail")
    never, never_dir = _demucs_job(tmp_path, "never", "0.5")

    r = _run_worker("demucs_worker", ["cpu"], fakes, tmp_path, f"{ok}\n{bad}\n{never}\n")

    assert r.returncode == 1
    lines = [
        ln
        for ln in r.stderr.splitlines()
        if ln.startswith("@@") and not ln.startswith(("@@PHASE@@", "@@SWEEPS@@"))
    ]
    assert lines[0] == "@@READY@@"
    assert [ln for ln in lines if not ln.startswith("@@SCALE@@")] == [
        "@@READY@@",
        "@@DONE@@",
        '@@ERROR@@"cannot decode this source"',
    ]
    # A worker in an unknown state never serves another job.
    assert not (never_dir / DEMUCS_MODEL).exists()


# A RoFormer pass in the worker, in this process: real torch tensors, a
# stand-in vocal model (it keeps 30% of what it hears) and a stand-in Demucs.


class _HalfModel:
    """Stands in for any model: records the device moves it was given."""

    def __init__(self, sources=(), samplerate=44100, audio_channels=2):
        self.sources = list(sources)
        self.samplerate = samplerate
        self.audio_channels = audio_channels
        self.moves: list[str] = []

    def cpu(self):
        self.moves.append("cpu")
        return self

    def to(self, device):
        self.moves.append(device)
        return self


def test_a_vocal_model_runs_on_the_mix_and_demucs_on_the_rest(monkeypatch):
    import torch

    from app.pipeline import demucs_worker, mel_roformer

    mix = torch.linspace(-0.5, 0.5, 2 * 3000).reshape(2, 3000)
    kim = _HalfModel()
    base = _HalfModel(_SIX)
    ft = _HalfModel(("drums", "bass", "other", "vocals"))
    heard: dict[str, torch.Tensor] = {}

    def fake_vocals(model, audio, device, progress=None):
        assert model is kim
        heard["kim"] = audio
        return audio * 0.3

    def fake_demucs(model, audio, device, shifts):
        heard["ft" if model is ft else "6s"] = audio
        factor = 10 if model is ft else 1
        return torch.stack([audio * (i + factor) / 100 for i in range(len(model.sources))])

    monkeypatch.setattr(mel_roformer, "separate_vocals", fake_vocals)
    monkeypatch.setattr(demucs_worker, "run_demucs", fake_demucs)
    monkeypatch.setattr(demucs_worker, "_load_mel_roformer", lambda model_id: kim)
    monkeypatch.setattr(demucs_worker, "load_demucs", lambda name: ft)
    models = demucs_worker.Models("htdemucs_6s", base)
    plan = {
        "vocals": "kim_mel_roformer",
        "drums": "htdemucs_6s",
        "bass": "htdemucs_ft",
        "guitar": "htdemucs_6s",
        "piano": "htdemucs_6s",
    }

    stems = demucs_worker.separate_plan(models, "cpu", mix, plan, 1)

    rest = mix * 0.7
    torch.testing.assert_close(heard["kim"], mix)
    torch.testing.assert_close(heard["6s"], rest)
    torch.testing.assert_close(heard["ft"], rest)
    torch.testing.assert_close(stems["vocals"], mix * 0.3)
    torch.testing.assert_close(stems["drums"], rest * 1 / 100)  # 6s source 0
    torch.testing.assert_close(stems["bass"], rest * 11 / 100)  # ft source 1
    torch.testing.assert_close(stems["guitar"], rest * 5 / 100)  # 6s source 4
    total = sum(stems.values())
    torch.testing.assert_close(total, mix, atol=1e-6, rtol=0)
    # The vocal model and ft went back to the CPU after their pass; the base
    # stays where it is.
    assert kim.moves == ["cpu", "cpu"]
    assert ft.moves == ["cpu"]
    assert base.moves == []
    assert models.loaded() == {"htdemucs_6s", "kim_mel_roformer", "htdemucs_ft"}

    # The next job without them drops both before it runs.
    demucs_worker.separate_plan(models, "cpu", mix, {s: "htdemucs_6s" for s in plan}, 1)
    assert models.loaded() == {"htdemucs_6s"}


def test_the_vocal_model_needs_44_1_khz_stereo(monkeypatch):
    import torch

    from app.pipeline import demucs_worker

    monkeypatch.setattr(demucs_worker, "_load_mel_roformer", lambda model_id: _HalfModel())
    models = demucs_worker.Models("htdemucs_6s", _HalfModel(_SIX, samplerate=8000))
    plan = {s: "htdemucs_6s" for s in ("drums", "bass", "guitar", "piano")}
    plan["vocals"] = "kim_mel_roformer"
    with pytest.raises(ValueError, match="44.1 kHz stereo"):
        demucs_worker.separate_plan(models, "cpu", torch.zeros(2, 100), plan, 1)


def test_demucs_worker_reports_a_model_it_cannot_download(tmp_path):
    """#125: the model's download from a host that accepts and then sends
    nothing ends in a worded error, not a worker that waits forever."""
    fakes = _write_pkg(
        tmp_path / "fakes",
        {
            **_FAKE_DEMUCS,
            "demucs/pretrained.py": """
                import os

                import torch.hub


                def get_model(name):
                    torch.hub.load_state_dict_from_url(os.environ["FAKE_MODEL_URL"], check_hash=True)
            """,
        },
    )
    job, job_dir = _demucs_job(tmp_path, "one", "0.5")
    with socket.create_server(("127.0.0.1", 0)) as server:
        url = f"http://127.0.0.1:{server.getsockname()[1]}/demucs/5c90dfd2-34c22ccb.th"
        accepted: list[socket.socket] = []

        def stall():
            # Accept, then never answer. Ends when the server socket closes.
            with contextlib.suppress(OSError):
                while True:
                    accepted.append(server.accept()[0])

        threading.Thread(target=stall, daemon=True).start()
        try:
            r = _run_worker(
                "demucs_worker",
                ["cpu"],
                fakes,
                tmp_path,
                job + "\n",
                extra_env={
                    "FAKE_MODEL_URL": url,
                    "TORCH_HOME": str(tmp_path / "torch"),
                    "STEMDECK_TIMEOUT_MODEL_DOWNLOAD_READ": "1",
                    "STEMDECK_MODEL_DOWNLOAD_ATTEMPTS": "2",
                },
            )
        finally:
            for conn in accepted:
                conn.close()

    assert r.returncode == 1
    lines = [ln for ln in r.stderr.splitlines() if ln.startswith("@@")]
    assert len(lines) == 1 and lines[0].startswith("@@ERROR@@"), r.stderr
    message = json.loads(lines[0][len("@@ERROR@@") :])
    assert message == (
        f"model download failed: {url};"
        f" put the file in {tmp_path / 'torch' / 'hub' / 'checkpoints'} (no data for 1 s)"
    )
    assert "model download try 1 of 2 failed (no data for 1 s), trying again in 2 s" in r.stderr
    assert not (job_dir / DEMUCS_MODEL).exists()


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


@pytest.mark.parametrize("device", ["cuda", "mps", "cpu"])
def test_a_model_is_released_back_to_the_cpu_with_its_gpu_cache(monkeypatch, device):
    import torch

    from app.pipeline import demucs_worker

    emptied: list[str] = []
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: emptied.append("cuda"))
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
    monkeypatch.setattr(torch.mps, "empty_cache", lambda: emptied.append("mps"))
    model = _HalfModel()

    demucs_worker._release(model, device)

    assert model.moves == ["cpu"]
    assert emptied == ([] if device == "cpu" else [device])


def test_each_pass_keeps_only_the_stems_it_is_for(monkeypatch):
    """ft's vocals and other, and 6s's vocals when the vocal model ran, are
    not kept until the end of the job; what is kept is a copy, not a view
    that holds the whole pass's output."""
    import torch

    from app.pipeline import demucs_worker, mel_roformer

    kept: dict[str, dict] = {}
    real_assemble = demucs_worker.stem_plan.assemble

    def spy(mix, plan, outputs):
        kept.update({k: dict(v) for k, v in outputs.items()})
        return real_assemble(mix, plan, outputs)

    out = torch.zeros(6, 2, 10)

    monkeypatch.setattr(demucs_worker.stem_plan, "assemble", spy)
    monkeypatch.setattr(mel_roformer, "separate_vocals", lambda m, a, d, p=None: a * 0.5)
    monkeypatch.setattr(demucs_worker, "run_demucs", lambda m, a, d, s: out[: len(m.sources)])
    monkeypatch.setattr(demucs_worker, "_load_mel_roformer", lambda model_id: _HalfModel())
    ft = _HalfModel(("drums", "bass", "other", "vocals"))
    monkeypatch.setattr(demucs_worker, "load_demucs", lambda name: ft)
    models = demucs_worker.Models("htdemucs_6s", _HalfModel(_SIX))
    plan = {
        "vocals": "kim_mel_roformer",
        "drums": "htdemucs_ft",
        "bass": "htdemucs_6s",
        "guitar": "htdemucs_6s",
        "piano": "htdemucs_6s",
    }

    demucs_worker.separate_plan(models, "cpu", torch.ones(2, 10), plan, 1)

    assert set(kept["htdemucs_6s"]) == {"bass", "guitar", "piano"}
    assert set(kept["htdemucs_ft"]) == {"drums"}
    for stems in (kept["htdemucs_6s"], kept["htdemucs_ft"]):
        for audio in stems.values():
            assert audio.untyped_storage().data_ptr() != out.untyped_storage().data_ptr()


# --- models the person added (My models) in the separation worker ----------------

USER_A = "user_0123456789ab"
USER_B = "user_ba9876543210"
_SHA = "a" * 64


def _user_entry(model_id: str, stems: list[str], outputs: dict[str, int], **over) -> dict:
    """A request line's description of a model the person added, as
    user_models.worker_spec gives it."""
    return {
        "id": model_id,
        "path": f"/nowhere/{model_id}/model.ckpt",
        "arch": "mel_band_roformer",
        "kwargs": {"num_stems": max(outputs.values()) + 1},
        "audio": {"chunk": 44100, "overlap": 2, "batch": 1},
        "outputs": outputs,
        "num_stems": max(outputs.values()) + 1,
        "sha256": _SHA,
        "size": 10,
        "mtime_ns": 1,
        "stems": stems,
        "format": "torch",
        **over,
    }


@pytest.fixture
def user_engine(monkeypatch):
    """Stand-ins for the RoFormer engine: a user model "keeps" a distinct share
    of what it hears per output index, 6s and ft keep shares of theirs. What
    each heard is recorded."""
    import torch

    from app.pipeline import demucs_worker, mel_roformer

    heard: dict[str, torch.Tensor] = {}
    asked: dict[str, tuple] = {}
    loaded: dict[str, _HalfModel] = {}

    def fake_load(model_id, spec, file):
        loaded[model_id] = _HalfModel()
        loaded[model_id].model_id = model_id
        return loaded[model_id]

    def fake_separate(model, audio, device, spec, progress=None, stems=None):
        heard[model.model_id] = audio
        asked[model.model_id] = tuple(stems)
        base = 0.05 if model.model_id == USER_A else 0.02
        return {s: audio * (base + 0.01 * spec.outputs[s]) for s in stems}

    def fake_demucs(model, audio, device, shifts):
        heard[getattr(model, "name", "6s")] = audio
        return torch.stack([audio * (i + 1) / 100 for i in range(len(model.sources))])

    monkeypatch.setattr(demucs_worker, "_load_user_model", fake_load)
    monkeypatch.setattr(mel_roformer, "separate", fake_separate)
    monkeypatch.setattr(mel_roformer, "separate_vocals", lambda m, a, d, p=None: a * 0.3)
    monkeypatch.setattr(demucs_worker, "run_demucs", fake_demucs)
    monkeypatch.setattr(demucs_worker, "_load_mel_roformer", lambda model_id: _HalfModel())
    ft = _HalfModel(("drums", "bass", "other", "vocals"))
    ft.name = "ft"
    monkeypatch.setattr(demucs_worker, "load_demucs", lambda name: ft)
    return heard, asked, loaded


def _user(entries: dict[str, dict]) -> dict:
    from app.pipeline import demucs_worker

    return {m: (s, f) for m, (s, f, _) in demucs_worker._user_models(entries).items()}


_E_SUM_CASES = [
    # user(drums, bass) + 6s
    (
        {
            "vocals": "htdemucs_6s",
            "drums": USER_A,
            "bass": USER_A,
            "guitar": "htdemucs_6s",
            "piano": "htdemucs_6s",
        },
        {USER_A: _user_entry(USER_A, ["drums", "bass"], {"drums": 1, "bass": 0})},
    ),
    # user(drums) + ft(bass) + 6s
    (
        {
            "vocals": "htdemucs_6s",
            "drums": USER_A,
            "bass": "htdemucs_ft",
            "guitar": "htdemucs_6s",
            "piano": "htdemucs_6s",
        },
        {USER_A: _user_entry(USER_A, ["drums"], {"drums": 0})},
    ),
    # two user models, and Kim
    (
        {
            "vocals": "kim_mel_roformer",
            "drums": USER_A,
            "bass": USER_B,
            "guitar": "htdemucs_6s",
            "piano": "htdemucs_6s",
        },
        {
            USER_A: _user_entry(USER_A, ["drums"], {"drums": 0}),
            USER_B: _user_entry(USER_B, ["bass"], {"bass": 0}),
        },
    ),
    # a user model that declares a stem the plan gave to 6s (bass)
    (
        {
            "vocals": "htdemucs_6s",
            "drums": USER_A,
            "bass": "htdemucs_6s",
            "guitar": "htdemucs_6s",
            "piano": "htdemucs_6s",
        },
        {USER_A: _user_entry(USER_A, ["drums", "bass"], {"drums": 1, "bass": 0})},
    ),
]


@pytest.mark.parametrize(("plan", "entries"), _E_SUM_CASES)
def test_the_stems_add_up_to_the_mix_whatever_a_user_model_makes(user_engine, plan, entries):
    """E-sum: other == mix - sum(kept) for every mix of engines, and a user
    model is asked for, and takes from the rest, only the stems the plan gave
    it."""
    import torch

    from app.pipeline import demucs_worker

    heard, asked, _ = user_engine
    mix = torch.linspace(-0.5, 0.5, 2 * 3000).reshape(2, 3000)
    models = demucs_worker.Models("htdemucs_6s", _HalfModel(_SIX))

    stems = demucs_worker.separate_plan(models, "cpu", mix, plan, 1, _user(entries))

    assert set(stems) == {"vocals", "drums", "bass", "guitar", "piano", "other"}
    kept = sum(v for k, v in stems.items() if k != "other")
    torch.testing.assert_close(stems["other"], mix - kept, atol=1e-6, rtol=0)
    torch.testing.assert_close(sum(stems.values()), mix, atol=1e-6, rtol=0)
    for model_id in entries:
        given = tuple(
            s for s in ("vocals", "drums", "bass", "guitar", "piano") if plan[s] == model_id
        )
        assert asked[model_id] == given
        torch.testing.assert_close(heard[model_id], mix)  # on the mix, never the rest
    # 6s hears the mix minus exactly the stems the RoFormer passes kept.
    first = [s for s in plan if plan[s] in entries or plan[s] == "kim_mel_roformer"]
    if first:
        rest = mix - sum(stems[s] for s in first)
        torch.testing.assert_close(heard["6s"], rest, atol=1e-6, rtol=0)


def test_a_user_model_that_cannot_load_fails_the_job_with_its_code(monkeypatch):
    import torch

    from app.pipeline import demucs_worker, mel_roformer

    said: list[str] = []
    monkeypatch.setattr(demucs_worker, "_say", said.append)

    def broken(model_id, spec, file):
        raise mel_roformer.GatesMissing()

    monkeypatch.setattr(demucs_worker, "_load_user_model", broken)
    models = demucs_worker.Models("htdemucs_6s", _HalfModel(_SIX))
    plan = {s: "htdemucs_6s" for s in ("vocals", "bass", "guitar", "piano")}
    plan["drums"] = USER_A
    entries = {USER_A: _user_entry(USER_A, ["drums"], {"drums": 0})}

    with pytest.raises(demucs_worker.UserModelFailed) as exc_info:
        demucs_worker.separate_plan(models, "cpu", torch.zeros(2, 100), plan, 1, _user(entries))

    assert str(exc_info.value) == f"user model failed: {USER_A}: gates_missing"
    assert any("to_gates" in line and USER_A in line for line in said)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (RuntimeError("CUDA out of memory. Tried 2 GiB"), "forward_failed"),
        (ValueError("the model returned audio of an unexpected shape"), "forward_failed"),
    ],
)
def test_a_user_model_that_fails_while_running_is_one_code(monkeypatch, failure, code):
    import torch

    from app.pipeline import demucs_worker, mel_roformer

    monkeypatch.setattr(demucs_worker, "_say", lambda line: None)
    model = _HalfModel()
    monkeypatch.setattr(demucs_worker, "_load_user_model", lambda m, s, f: model)

    def fails(*a, **kw):
        raise failure

    monkeypatch.setattr(mel_roformer, "separate", fails)
    models = demucs_worker.Models("htdemucs_6s", _HalfModel(_SIX))
    plan = dict.fromkeys(("vocals", "bass", "guitar", "piano"), "htdemucs_6s")
    plan["drums"] = USER_A
    entries = {USER_A: _user_entry(USER_A, ["drums"], {"drums": 0})}

    with pytest.raises(demucs_worker.UserModelFailed) as exc_info:
        demucs_worker.separate_plan(models, "cuda", torch.zeros(2, 100), plan, 1, _user(entries))
    assert exc_info.value.code == code
    # Back on the CPU even after the failure.
    assert model.moves == ["cuda", "cpu"]


def test_a_user_model_that_gives_samples_that_are_not_numbers_is_non_finite(monkeypatch):
    """S-6: an fp16 overflow on a GPU is this model's failure, so the job
    stands 6s in, instead of the generic "not numbers" failure at the write."""
    import torch

    from app.pipeline import demucs_worker, mel_roformer

    monkeypatch.setattr(demucs_worker, "_say", lambda line: None)
    monkeypatch.setattr(demucs_worker, "_load_user_model", lambda m, s, f: _HalfModel())
    monkeypatch.setattr(
        mel_roformer,
        "separate",
        lambda model, audio, device, spec, progress=None, stems=None: {
            s: audio * float("nan") for s in stems
        },
    )
    models = demucs_worker.Models("htdemucs_6s", _HalfModel(_SIX))
    plan = dict.fromkeys(("vocals", "bass", "guitar", "piano"), "htdemucs_6s")
    plan["drums"] = USER_A
    entries = {USER_A: _user_entry(USER_A, ["drums"], {"drums": 0})}

    with pytest.raises(demucs_worker.UserModelFailed) as exc_info:
        demucs_worker.separate_plan(models, "cpu", torch.ones(2, 100), plan, 1, _user(entries))
    assert exc_info.value.code == "non_finite"


def test_a_user_model_is_loaded_again_for_other_bytes_and_dropped_when_unused(user_engine):
    import torch

    from app.pipeline import demucs_worker

    _, _, loaded = user_engine
    models = demucs_worker.Models("htdemucs_6s", _HalfModel(_SIX))
    plan = dict.fromkeys(("vocals", "bass", "guitar", "piano"), "htdemucs_6s")
    plan["drums"] = USER_A
    mix = torch.zeros(2, 100)
    entries = {USER_A: _user_entry(USER_A, ["drums"], {"drums": 0})}
    demucs_worker.separate_plan(models, "cpu", mix, plan, 1, _user(entries))
    first = loaded[USER_A]
    demucs_worker.separate_plan(models, "cpu", mix, plan, 1, _user(entries))
    assert loaded[USER_A] is first, "the same bytes: kept loaded"
    other = {USER_A: _user_entry(USER_A, ["drums"], {"drums": 0}, sha256="b" * 64)}
    demucs_worker.separate_plan(models, "cpu", mix, plan, 1, _user(other))
    assert loaded[USER_A] is not first
    demucs_worker.separate_plan(models, "cpu", mix, dict.fromkeys(plan, "htdemucs_6s"), 1)
    assert models.loaded() == {"htdemucs_6s"}


def test_a_user_id_is_never_loaded_as_demucs(monkeypatch):
    from app.pipeline import demucs_worker

    monkeypatch.setattr(demucs_worker, "load_demucs", lambda name: pytest.fail("ran as Demucs"))
    models = demucs_worker.Models("htdemucs_6s", _HalfModel(_SIX))
    with pytest.raises(ValueError):
        models.get(USER_A)


def test_demucs_worker_says_a_user_model_failure_on_its_last_line_and_exits(tmp_path):
    """The parent stands 6s in only on this exact last line."""
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    plan = {
        "vocals": "htdemucs_6s",
        "drums": USER_A,
        "bass": "htdemucs_6s",
        "guitar": "htdemucs_6s",
        "piano": "htdemucs_6s",
    }
    entries = {USER_A: _user_entry(USER_A, ["drums"], {"drums": 0})}
    job, job_dir = _demucs_job(tmp_path, "one", "0.5", plan=plan, user_models=entries)
    second, _ = _demucs_job(tmp_path, "two", "0.5")

    r = _run_worker(
        "demucs_worker",
        ["cpu"],
        fakes,
        tmp_path,
        f"{job}\n{second}\n",
        {"FAKE_SOURCES": ",".join(_SIX)},
    )

    assert r.returncode == 1
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    # The fake base model is 8 kHz: the user model cannot run on it.
    assert lines[-1] == "@@ERROR@@" + json.dumps(f"user model failed: {USER_A}: unsupported_audio")
    assert lines[-2].startswith(f"user model {USER_A} could not run (unsupported_audio)")
    assert "@@DONE@@" not in r.stderr, "the worker exits; the second job is not served"
    assert not (job_dir / DEMUCS_MODEL).exists()


def test_demucs_worker_refuses_a_user_model_its_request_does_not_describe(tmp_path):
    fakes = _write_pkg(tmp_path / "fakes", _FAKE_DEMUCS)
    plan = {
        "vocals": "htdemucs_6s",
        "drums": USER_A,
        "bass": "htdemucs_6s",
        "guitar": "htdemucs_6s",
        "piano": "htdemucs_6s",
    }
    job, _ = _demucs_job(tmp_path, "one", "0.5", plan=plan)

    r = _run_worker("demucs_worker", ["cpu"], fakes, tmp_path, job + "\n")

    assert r.returncode == 1
    last = [ln for ln in r.stderr.splitlines() if ln.strip()][-1]
    assert last == "@@ERROR@@" + json.dumps("the separation plan has no usable model for drums")
