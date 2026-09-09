"""The stdout/stderr contract every separation worker speaks.

The three workers run as their own processes, so nothing in the app ever calls
them directly and a mistake in the protocol is invisible until a real
separation hangs or reports the wrong thing. The parent (separate.py,
vocal_split.py, sections.py) reads stderr looking for exactly "@@DONE@@" or an
"@@ERROR@@"-prefixed JSON message, and treats a worker that exits without
either as a crash -- so the marker, the exit code and the "does the worker keep
serving?" decision are load-bearing, and all three are asserted here.

The heavy dependencies (demucs, audio-separator, all-in-one) are stubbed. The
point is the protocol and the argument handling around it, not the inference:
loading a real model would download hundreds of megabytes of weights to prove
nothing this file is about.
"""

from __future__ import annotations

import io
import json
import sys
import types

import pytest

from app.core.config import DEMUCS_MODEL


@pytest.fixture(autouse=True)
def _no_parent_watchdog(monkeypatch):
    """Every worker arms the parent-death watchdog first thing. It is off unless
    STEMDECK_PARENT_PID is set, and a stray value in the environment running the
    suite would start a thread that kills the test process."""
    monkeypatch.delenv("STEMDECK_PARENT_PID", raising=False)


def _feed_stdin(monkeypatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


# --------------------------------------------------------------------------
# demucs_worker -- the persistent one. It is the only worker that survives a
# job, so "keeps serving after success, dies after failure" is its whole point.
# --------------------------------------------------------------------------


class _FakeModel:
    audio_channels = 2
    samplerate = 44100
    sources = ["drums", "bass", "other", "vocals"]

    def __init__(self):
        self.evaled = False
        self.moved_to_cpu = False

    def eval(self):
        self.evaled = True
        return self

    def cpu(self):
        self.moved_to_cpu = True
        return self


def _install_fake_demucs(monkeypatch, *, model=None, apply_model=None, save_audio=None):
    """Stand in for the four demucs entry points the worker imports.

    They are imported inside the functions, so replacing the modules in
    sys.modules is enough -- no import of the real package happens at all.
    """
    import torch

    calls: dict[str, list] = {"apply": [], "save": [], "load_track": [], "get_model": []}
    model = model if model is not None else _FakeModel()

    def _default_apply(m, wav, **kw):
        calls["apply"].append({"wav": wav, **kw})
        # (batch, source, channel, sample), which the worker indexes [0] on.
        return torch.zeros(1, len(m.sources), wav.shape[1], wav.shape[2])

    def _default_save(tensor, path, **kw):
        calls["save"].append({"path": path, **kw})
        # Write something so the test can assert against the filesystem rather
        # than only against the mock.
        with open(path, "wb") as fh:
            fh.write(b"RIFF")

    def _load_track(source, channels, samplerate):
        calls["load_track"].append((source, channels, samplerate))
        torch.manual_seed(0)
        return torch.randn(channels, 4096) * 3 + 7

    def _get_model(name):
        calls["get_model"].append(name)
        return model

    mods = {
        "demucs": types.ModuleType("demucs"),
        "demucs.apply": types.ModuleType("demucs.apply"),
        "demucs.audio": types.ModuleType("demucs.audio"),
        "demucs.separate": types.ModuleType("demucs.separate"),
        "demucs.pretrained": types.ModuleType("demucs.pretrained"),
    }
    mods["demucs.apply"].apply_model = apply_model or _default_apply
    mods["demucs.audio"].save_audio = save_audio or _default_save
    mods["demucs.separate"].load_track = _load_track
    mods["demucs.pretrained"].get_model = _get_model
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return model, calls


def test_a_finished_job_writes_one_wav_per_source_under_the_model_dir(monkeypatch, tmp_path):
    from app.pipeline import demucs_worker

    model, calls = _install_fake_demucs(monkeypatch)
    source = tmp_path / "song.wav"
    source.write_bytes(b"")
    job_dir = tmp_path / "job"

    demucs_worker._run_one_job(model, "cpu", {"source": str(source), "job_dir": str(job_dir)})

    out_dir = job_dir / DEMUCS_MODEL / "song"
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "bass.wav",
        "drums.wav",
        "other.wav",
        "vocals.wav",
    ]


def test_stems_are_written_at_the_models_own_samplerate_and_bit_depth(monkeypatch, tmp_path):
    """demucs.separate's CLI writes 16-bit integer WAVs, and the rest of the app
    (ffmpeg export, the browser decoder) is built for that. A change here would
    be silent until someone opened a stem."""
    from app.pipeline import demucs_worker

    model, calls = _install_fake_demucs(monkeypatch)
    source = tmp_path / "song.wav"
    source.write_bytes(b"")

    demucs_worker._run_one_job(model, "cpu", {"source": str(source), "job_dir": str(tmp_path)})

    assert calls["save"], "no stem was written"
    for saved in calls["save"]:
        assert saved["samplerate"] == model.samplerate
        assert saved["bits_per_sample"] == 16
        assert saved["as_float"] is False
        assert saved["clip"] == "rescale"


def test_the_track_is_loaded_with_the_models_own_channel_count_and_rate(monkeypatch, tmp_path):
    from app.pipeline import demucs_worker

    model, calls = _install_fake_demucs(monkeypatch)
    source = tmp_path / "song.wav"
    source.write_bytes(b"")

    demucs_worker._run_one_job(model, "cpu", {"source": str(source), "job_dir": str(tmp_path)})

    assert calls["load_track"] == [(source, model.audio_channels, model.samplerate)]


def test_the_input_is_normalised_before_inference_and_undone_after(monkeypatch, tmp_path):
    """demucs.separate.main() centres and scales the track before apply_model
    and reverses it on the output. Dropping either half does not crash -- it
    quietly changes every stem's level, which is exactly the kind of bug that
    reaches a release."""
    import torch

    from app.pipeline import demucs_worker

    seen = {}

    def _apply(m, wav, **kw):
        seen["wav"] = wav.clone()
        # Hand back the normalised input as every "source" so the test can check
        # the de-normalisation applied on the way out.
        return wav.expand(1, len(m.sources), *wav.shape[1:]).clone()

    written = {}

    def _save(tensor, path, **kw):
        written[path.rsplit("/", 1)[-1]] = tensor.clone()
        open(path, "wb").close()

    model, calls = _install_fake_demucs(monkeypatch, apply_model=_apply, save_audio=_save)
    source = tmp_path / "song.wav"
    source.write_bytes(b"")

    demucs_worker._run_one_job(model, "cpu", {"source": str(source), "job_dir": str(tmp_path)})

    torch.manual_seed(0)
    original = torch.randn(model.audio_channels, 4096) * 3 + 7
    # The reference is the mono mixdown, not the stereo signal -- centring and
    # scaling by that is what demucs.separate.main() does, and the stems come
    # back on that scale.
    ref = original.mean(0)
    expected = (original - ref.mean()) / ref.std()

    assert torch.allclose(seen["wav"][0], expected, atol=1e-5)
    # Whatever the loud original was, it is centred and scaled on the way in.
    assert abs(float(seen["wav"][0].mean())) < abs(float(original.mean()))
    assert float(seen["wav"][0].std()) < float(original.std())

    # And what was written was mapped back out of that space, so it is the
    # original loud signal again rather than the normalised one.
    restored = next(iter(written.values()))
    assert torch.allclose(restored, original, atol=1e-4)


def test_shifts_is_passed_through_and_defaults_to_one(monkeypatch, tmp_path):
    """shifts is the quality setting the user picks; silently ignoring it would
    make the "best" preset identical to the fastest one."""
    from app.pipeline import demucs_worker

    source = tmp_path / "song.wav"
    source.write_bytes(b"")

    model, calls = _install_fake_demucs(monkeypatch)
    demucs_worker._run_one_job(
        model, "cpu", {"source": str(source), "job_dir": str(tmp_path), "shifts": 5}
    )
    assert calls["apply"][0]["shifts"] == 5

    model, calls = _install_fake_demucs(monkeypatch)
    demucs_worker._run_one_job(model, "cpu", {"source": str(source), "job_dir": str(tmp_path)})
    assert calls["apply"][0]["shifts"] == 1


def test_the_requested_device_reaches_apply_model(monkeypatch, tmp_path):
    from app.pipeline import demucs_worker

    model, calls = _install_fake_demucs(monkeypatch)
    source = tmp_path / "song.wav"
    source.write_bytes(b"")

    demucs_worker._run_one_job(model, "cuda", {"source": str(source), "job_dir": str(tmp_path)})

    assert calls["apply"][0]["device"] == "cuda"


def test_a_successful_job_says_done_and_the_worker_keeps_serving(monkeypatch, tmp_path, capsys):
    """The reason this worker exists: one model load, many jobs."""
    from app.pipeline import demucs_worker

    model, calls = _install_fake_demucs(monkeypatch)
    source = tmp_path / "song.wav"
    source.write_bytes(b"")
    req = json.dumps({"source": str(source), "job_dir": str(tmp_path)})
    monkeypatch.setattr(sys, "argv", ["demucs_worker", "cpu"])
    _feed_stdin(monkeypatch, f"{req}\n{req}\n")

    demucs_worker.main()

    err = capsys.readouterr().err
    assert err.count("@@DONE@@") == 2
    assert "@@ERROR@@" not in err
    # One load for two jobs is the entire point of the persistent worker.
    assert calls["get_model"] == [DEMUCS_MODEL]


def test_the_model_is_loaded_once_in_eval_mode_on_the_cpu(monkeypatch, tmp_path, capsys):
    """apply_model moves the weights to the device itself; loading them onto the
    GPU here would double the VRAM the model occupies."""
    from app.pipeline import demucs_worker

    model, calls = _install_fake_demucs(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["demucs_worker", "cuda"])
    _feed_stdin(monkeypatch, "")

    demucs_worker.main()

    assert model.evaled is True
    assert model.moved_to_cpu is True


def test_a_failing_job_reports_the_message_and_exits(monkeypatch, tmp_path, capsys):
    """A failure has to kill the worker: after an exception mid-inference the
    CUDA context is not something the next job can trust."""
    from app.pipeline import demucs_worker

    def _boom(*a, **kw):
        raise RuntimeError("CUDA out of memory")

    _install_fake_demucs(monkeypatch, apply_model=_boom)
    source = tmp_path / "song.wav"
    source.write_bytes(b"")
    req = json.dumps({"source": str(source), "job_dir": str(tmp_path)})
    monkeypatch.setattr(sys, "argv", ["demucs_worker", "cpu"])
    # A second job queued behind the failure must never be picked up.
    _feed_stdin(monkeypatch, f"{req}\n{req}\n")

    with pytest.raises(SystemExit) as exc:
        demucs_worker.main()

    assert exc.value.code == 1
    out = capsys.readouterr().err
    assert "@@DONE@@" not in out
    line = next(x for x in out.splitlines() if x.startswith("@@ERROR@@"))
    # The payload is JSON so the parent can carry a message containing quotes,
    # newlines or a Windows path back to the UI intact.
    assert json.loads(line.removeprefix("@@ERROR@@")) == "CUDA out of memory"


def test_a_malformed_request_line_is_an_error_not_a_crash(monkeypatch, capsys):
    from app.pipeline import demucs_worker

    _install_fake_demucs(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["demucs_worker", "cpu"])
    _feed_stdin(monkeypatch, "{not json\n")

    with pytest.raises(SystemExit) as exc:
        demucs_worker.main()

    assert exc.value.code == 1
    assert "@@ERROR@@" in capsys.readouterr().err


def test_blank_lines_are_ignored_rather_than_answered(monkeypatch, tmp_path, capsys):
    """A stray newline on the pipe must not produce a @@DONE@@ the parent would
    credit to a job that never ran."""
    from app.pipeline import demucs_worker

    _install_fake_demucs(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["demucs_worker", "cpu"])
    _feed_stdin(monkeypatch, "\n   \n\n")

    demucs_worker.main()

    assert capsys.readouterr().err == ""


def test_eof_on_stdin_ends_the_worker_cleanly(monkeypatch, capsys):
    """The parent closing the pipe is the normal shutdown, not a failure."""
    from app.pipeline import demucs_worker

    _install_fake_demucs(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["demucs_worker", "cpu"])
    _feed_stdin(monkeypatch, "")

    demucs_worker.main()  # returns rather than raising SystemExit

    assert "@@ERROR@@" not in capsys.readouterr().err


def test_the_device_defaults_to_cpu_when_argv_carries_none(monkeypatch, tmp_path, capsys):
    from app.pipeline import demucs_worker

    model, calls = _install_fake_demucs(monkeypatch)
    source = tmp_path / "song.wav"
    source.write_bytes(b"")
    monkeypatch.setattr(sys, "argv", ["demucs_worker"])
    _feed_stdin(monkeypatch, json.dumps({"source": str(source), "job_dir": str(tmp_path)}) + "\n")

    demucs_worker.main()

    assert calls["apply"][0]["device"] == "cpu"


# --------------------------------------------------------------------------
# vocal_split_worker -- one shot, same markers.
# --------------------------------------------------------------------------


class _FakeSeparator:
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.loaded: list = []
        self.separated: list = []
        _FakeSeparator.instances.append(self)

    def load_model(self, model_filename):
        self.loaded.append(model_filename)

    def separate(self, path, names):
        self.separated.append((path, names))


def _install_fake_separator(monkeypatch, separator_cls=None):
    _FakeSeparator.instances = []
    pkg = types.ModuleType("audio_separator")
    mod = types.ModuleType("audio_separator.separator")
    mod.Separator = separator_cls or _FakeSeparator
    monkeypatch.setitem(sys.modules, "audio_separator", pkg)
    monkeypatch.setitem(sys.modules, "audio_separator.separator", mod)
    return _FakeSeparator


def test_a_completed_split_says_done(monkeypatch, tmp_path, capsys):
    from app.pipeline import vocal_split_worker

    _install_fake_separator(monkeypatch)
    monkeypatch.setattr(
        sys, "argv", ["vocal_split_worker", "cpu", str(tmp_path / "vocals.wav"), str(tmp_path)]
    )

    vocal_split_worker.main()

    assert capsys.readouterr().err.strip() == "@@DONE@@"


def test_the_two_outputs_are_renamed_at_the_library_boundary(monkeypatch, tmp_path, capsys):
    """The karaoke model calls its outputs Vocals/Instrumental. The rest of the
    app knows them as lead_vocals/backing_vocals, and the rename is the only
    thing keeping those two vocabularies apart."""
    from app.pipeline import vocal_split_worker

    _install_fake_separator(monkeypatch)
    vocals = tmp_path / "vocals.wav"
    monkeypatch.setattr(sys, "argv", ["vocal_split_worker", "cpu", str(vocals), str(tmp_path)])

    vocal_split_worker.main()

    sep = _FakeSeparator.instances[-1]
    assert sep.separated == [
        (str(vocals), {"Vocals": "lead_vocals", "Instrumental": "backing_vocals"})
    ]
    assert sep.kwargs["output_dir"] == str(tmp_path)
    assert sep.kwargs["output_format"] == "WAV"


def test_asking_for_the_cpu_hides_the_gpu_from_onnxruntime(monkeypatch, tmp_path, capsys):
    from app.pipeline import vocal_split_worker

    _install_fake_separator(monkeypatch)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["vocal_split_worker", "cpu", str(tmp_path / "v.wav"), str(tmp_path)]
    )

    vocal_split_worker.main()

    import os

    assert os.environ["CUDA_VISIBLE_DEVICES"] == ""


def test_a_gpu_split_leaves_the_device_visibility_alone(monkeypatch, tmp_path, capsys):
    from app.pipeline import vocal_split_worker

    _install_fake_separator(monkeypatch)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["vocal_split_worker", "cuda", str(tmp_path / "v.wav"), str(tmp_path)]
    )

    vocal_split_worker.main()

    import os

    assert "CUDA_VISIBLE_DEVICES" not in os.environ


def test_a_split_that_raises_reports_the_message_and_exits(monkeypatch, tmp_path, capsys):
    from app.pipeline import vocal_split_worker

    class _Boom(_FakeSeparator):
        def separate(self, path, names):
            raise RuntimeError("onnx session failed")

    _install_fake_separator(monkeypatch, _Boom)
    monkeypatch.setattr(
        sys, "argv", ["vocal_split_worker", "cpu", str(tmp_path / "v.wav"), str(tmp_path)]
    )

    with pytest.raises(SystemExit) as exc:
        vocal_split_worker.main()

    assert exc.value.code == 1
    line = next(x for x in capsys.readouterr().err.splitlines() if x.startswith("@@ERROR@@"))
    assert json.loads(line.removeprefix("@@ERROR@@")) == "onnx session failed"


def test_missing_arguments_are_refused_before_anything_is_loaded(monkeypatch, capsys):
    from app.pipeline import vocal_split_worker

    monkeypatch.setattr(sys, "argv", ["vocal_split_worker", "cpu"])

    with pytest.raises(SystemExit) as exc:
        vocal_split_worker.main()

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("@@ERROR@@usage:")


def test_the_model_load_goes_through_the_healing_wrapper(monkeypatch, tmp_path, capsys):
    """A checkpoint truncated by a dropped download stays broken forever unless
    the load is routed through load_or_heal (#502)."""
    from app.pipeline import vocal_split_worker

    _install_fake_separator(monkeypatch)
    seen = {}

    def _fake_load_or_heal(load, stale):
        seen["stale_paths"] = list(stale())
        return load()

    monkeypatch.setattr("app.core.model_cache.load_or_heal", _fake_load_or_heal)
    monkeypatch.setattr(
        sys, "argv", ["vocal_split_worker", "cpu", str(tmp_path / "v.wav"), str(tmp_path)]
    )

    vocal_split_worker.main()

    assert _FakeSeparator.instances[-1].loaded, "the model was never loaded"
    # The index files matter as much as the checkpoint: a truncated model is
    # reported as an unknown MD5, which is a lookup against them.
    names = {p.name for p in seen["stale_paths"]}
    assert "vr_model_data.json" in names
    assert "mdx_model_data.json" in names


# --------------------------------------------------------------------------
# section_worker -- speaks one JSON line on stdout instead of a marker, and is
# the only worker that parses arguments.
# --------------------------------------------------------------------------


class _Segment:
    def __init__(self, start, end, label):
        self.start = start
        self.end = end
        self.label = label


class _Result:
    def __init__(self, segments):
        self.segments = segments


def test_segments_are_coerced_to_plain_json_types():
    """numpy floats and label enums serialise to something json.dumps refuses or
    the frontend cannot read; the coercion is what keeps the contract simple."""
    from app.pipeline import section_worker

    out = section_worker._result_segments(_Result([_Segment("0", "4.5", 7)]))

    assert out == [{"start": 0.0, "end": 4.5, "label": "7"}]
    assert isinstance(out[0]["start"], float)
    assert isinstance(out[0]["label"], str)


def test_a_single_element_batch_is_unwrapped():
    from app.pipeline import section_worker

    out = section_worker._result_segments([_Result([_Segment(0, 1, "intro")])])

    assert out == [{"start": 0.0, "end": 1.0, "label": "intro"}]


def test_a_batch_that_is_not_one_result_is_refused():
    from app.pipeline import section_worker

    with pytest.raises(RuntimeError, match="unexpected result count"):
        section_worker._result_segments([_Result([]), _Result([])])


def test_a_result_without_segments_is_refused():
    from app.pipeline import section_worker

    with pytest.raises(RuntimeError, match="no segments"):
        section_worker._result_segments(object())

    with pytest.raises(RuntimeError, match="no segments"):
        section_worker._result_segments(_Result("not-a-list"))


def test_an_absent_beat_grid_is_simply_absent(tmp_path):
    from app.pipeline import section_worker

    assert section_worker._load_beat_grid(None) is None
    assert section_worker._load_beat_grid(tmp_path / "nope.json") is None
    # A directory is not a file, and must not raise on the way to that answer.
    assert section_worker._load_beat_grid(tmp_path) is None


def test_an_unreadable_beat_grid_degrades_instead_of_failing(tmp_path, capsys):
    """Refinement is an improvement on the raw segments, never a precondition:
    a corrupt grid must cost accuracy, not the whole analysis."""
    from app.pipeline import section_worker

    bad = tmp_path / "beats.json"
    bad.write_text("{not json", encoding="utf-8")

    assert section_worker._load_beat_grid(bad) is None
    assert "unreadable beat grid" in capsys.readouterr().err


def test_a_readable_beat_grid_is_parsed(tmp_path):
    from app.pipeline import section_worker

    grid = tmp_path / "beats.json"
    grid.write_text(json.dumps({"beats": [0.5, 1.0]}), encoding="utf-8")

    assert section_worker._load_beat_grid(grid) == {"beats": [0.5, 1.0]}


def test_the_beat_grid_argument_is_optional_but_the_rest_are_not(tmp_path):
    from app.pipeline import section_worker

    args = section_worker._parser().parse_args(
        ["--stems-dir", str(tmp_path), "--identifier", "abc", "--model", "harmonix-all"]
    )
    assert args.beat_grid is None
    assert args.identifier == "abc"

    with pytest.raises(SystemExit):
        section_worker._parser().parse_args(["--identifier", "abc"])


def test_a_missing_stem_stops_the_run_before_the_model_is_loaded(monkeypatch, tmp_path):
    """Loading the model first would spend seconds and a download to arrive at
    the same failure."""
    from app.pipeline import section_worker

    stems = tmp_path / "stems"
    stems.mkdir()
    for name in ("bass", "drums", "other"):  # vocals.wav deliberately absent
        (stems / f"{name}.wav").write_bytes(b"")

    with pytest.raises(FileNotFoundError, match="required section-analysis stem"):
        section_worker.main(
            ["--stems-dir", str(stems), "--identifier", "abc", "--model", "harmonix-all"]
        )


def _install_fake_allin1(monkeypatch, result, *, labels=("intro", "verse", "chorus")):
    """Stand in for the all-in-one inference stack the section worker imports.

    Imported inside main() and behind a redirect_stdout, so swapping the modules
    out is enough. Loading the real thing means a model download.
    """
    calls: dict[str, list] = {"spec": [], "model": [], "inference": []}

    def _extract_spectrograms(dirs, out, multiprocess):
        calls["spec"].append((list(dirs), out, multiprocess))
        # Third-party code is chatty on stdout; the worker redirects it and this
        # proves the redirect is what keeps the JSON line clean.
        print("allin1: extracting spectrograms")
        return [out / "song.npy"]

    def _load_pretrained_model(model_name, device):
        calls["model"].append((model_name, device))
        return object()

    def _run_inference(**kw):
        calls["inference"].append(kw)
        return result

    pkg = types.ModuleType("allin1_infer")
    config = types.ModuleType("allin1_infer.config")
    config.HARMONIX_LABELS = list(labels)
    helpers = types.ModuleType("allin1_infer.helpers")
    helpers.run_inference = _run_inference
    models = types.ModuleType("allin1_infer.models")
    models.load_pretrained_model = _load_pretrained_model
    spectrogram = types.ModuleType("allin1_infer.spectrogram")
    spectrogram.extract_spectrograms = _extract_spectrograms
    for name, mod in {
        "allin1_infer": pkg,
        "allin1_infer.config": config,
        "allin1_infer.helpers": helpers,
        "allin1_infer.models": models,
        "allin1_infer.spectrogram": spectrogram,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return calls


def _stems(tmp_path):
    stems = tmp_path / "stems"
    stems.mkdir()
    for name in ("bass", "drums", "other", "vocals"):
        (stems / f"{name}.wav").write_bytes(b"")
    return stems


def test_the_analysis_prints_exactly_one_compact_json_line_on_stdout(monkeypatch, tmp_path, capsys):
    """The parent parses stdout as a single JSON document. Anything the model
    stack prints has to be diverted, or the section data is unreadable."""
    from app.pipeline import section_worker

    result = _Result([_Segment(0, 12.5, "intro"), _Segment(12.5, 30, "verse")])
    _install_fake_allin1(monkeypatch, result)
    monkeypatch.setattr(section_worker, "refine_segments", lambda *a, **k: a[0])

    rc = section_worker.main(
        ["--stems-dir", str(_stems(tmp_path)), "--identifier", "abc", "--model", "harmonix-all"]
    )

    assert rc == 0
    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"stdout was not a single line: {lines}"
    assert json.loads(lines[0]) == {
        "segments": [
            {"start": 0.0, "end": 12.5, "label": "intro"},
            {"start": 12.5, "end": 30.0, "label": "verse"},
        ]
    }
    # The library's chatter went to stderr instead of corrupting the payload.
    assert "extracting spectrograms" in captured.err


def test_refinement_failure_falls_back_to_the_raw_segments(monkeypatch, tmp_path, capsys):
    """Refinement sharpens boundaries; it is not allowed to lose the analysis."""
    from app.pipeline import section_worker

    _install_fake_allin1(monkeypatch, _Result([_Segment(0, 9, "chorus")]))

    def _boom(*a, **kw):
        raise ValueError("bad activations")

    monkeypatch.setattr(section_worker, "refine_segments", _boom)

    rc = section_worker.main(
        ["--stems-dir", str(_stems(tmp_path)), "--identifier", "abc", "--model", "harmonix-all"]
    )

    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads([ln for ln in captured.out.splitlines() if ln.strip()][0])
    assert payload == {"segments": [{"start": 0.0, "end": 9.0, "label": "chorus"}]}
    assert "section refinement fell back after ValueError" in captured.err


def test_refinement_is_handed_the_models_own_activations_and_labels(monkeypatch, tmp_path, capsys):
    from app.pipeline import section_worker

    result = _Result([_Segment(0, 9, "chorus")])
    result.activations = {"beat": [0.1]}
    result.embeddings = [[0.2]]
    result.activation_fps = 100
    _install_fake_allin1(monkeypatch, result, labels=("intro", "verse"))

    grid = tmp_path / "beats.json"
    grid.write_text(json.dumps({"beats": [0.5]}), encoding="utf-8")

    seen = {}

    def _capture(raw, activations, embeddings, fps, beat_grid, labels):
        seen.update(
            raw=raw,
            activations=activations,
            embeddings=embeddings,
            fps=fps,
            beat_grid=beat_grid,
            labels=labels,
        )
        return raw

    monkeypatch.setattr(section_worker, "refine_segments", _capture)

    section_worker.main(
        [
            "--stems-dir",
            str(_stems(tmp_path)),
            "--identifier",
            "abc",
            "--model",
            "harmonix-all",
            "--beat-grid",
            str(grid),
        ]
    )

    assert seen["activations"] == {"beat": [0.1]}
    assert seen["embeddings"] == [[0.2]]
    assert seen["fps"] == 100
    assert seen["beat_grid"] == {"beats": [0.5]}
    assert seen["labels"] == ["intro", "verse"]


def test_inference_runs_on_the_cpu_with_the_requested_model(monkeypatch, tmp_path, capsys):
    """The worker exists so section analysis cannot contend with the GPU the
    separation stage is using."""
    from app.pipeline import section_worker

    calls = _install_fake_allin1(monkeypatch, _Result([_Segment(0, 1, "intro")]))
    monkeypatch.setattr(section_worker, "refine_segments", lambda *a, **k: a[0])
    stems = _stems(tmp_path)

    section_worker.main(
        ["--stems-dir", str(stems), "--identifier", "song-id", "--model", "harmonix-all"]
    )

    assert calls["model"] == [("harmonix-all", "cpu")]
    assert calls["spec"][0][0] == [stems]
    assert calls["spec"][0][2] is False, "multiprocess must stay off inside a worker process"
    inference = calls["inference"][0]
    assert inference["device"] == "cpu"
    assert inference["path"].name == "song-id.wav"
    assert inference["include_activations"] is True
    assert inference["include_embeddings"] is True


def test_the_heartbeat_thread_is_stopped_even_when_the_run_fails(monkeypatch, tmp_path, capsys):
    """The heartbeat is what tells the parent a slow CPU pass is still alive. A
    thread left running past a failure would keep the process from exiting."""
    import threading

    from app.pipeline import section_worker

    _install_fake_allin1(monkeypatch, _Result("not-a-list"))
    before = threading.active_count()

    with pytest.raises(RuntimeError, match="no segments"):
        section_worker.main(
            ["--stems-dir", str(_stems(tmp_path)), "--identifier", "a", "--model", "m"]
        )

    # join(timeout=2) in the finally block: the thread is gone, not leaked.
    assert threading.active_count() <= before
