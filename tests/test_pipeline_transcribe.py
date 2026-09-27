"""Lyrics transcription (app/pipeline/transcribe.py) and its setting.

No model is ever loaded here: the worker is a stub script behind the
_spawn_worker_cmd seam, and the worker's own helpers are tested on synthetic
audio. conftest's _no_whisper keeps every other test away from the real one.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import settings as settings_mod
from app.core.models import Job, JobCancelled
from app.core.registry import get_proc
from app.pipeline import transcribe as tr
from app.pipeline import transcribe_worker as worker
from app.pipeline.lyrics_lookup import lyrics_path, read_lyrics

# ── the setting ──


def test_setting_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("STEMDECK_TRANSCRIBE_LYRICS", raising=False)
    assert settings_mod.get_transcribe_lyrics() == "auto"


def test_setting_env_seeds_default(monkeypatch):
    monkeypatch.setenv("STEMDECK_TRANSCRIBE_LYRICS", "off")
    assert settings_mod.get_transcribe_lyrics() == "off"


def test_setting_rejects_unknown_choice(monkeypatch):
    monkeypatch.delenv("STEMDECK_TRANSCRIBE_LYRICS", raising=False)
    with pytest.raises(ValueError):
        settings_mod.set_transcribe_lyrics("always")
    assert settings_mod.get_transcribe_lyrics() == "auto"  # nothing persisted


def test_setting_api_round_trip_and_422(monkeypatch):
    monkeypatch.delenv("STEMDECK_TRANSCRIBE_LYRICS", raising=False)
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/settings").json()["transcribe_lyrics"] == "auto"
        r = c.post("/api/settings", json={"transcribe_lyrics": "on"})
        assert r.status_code == 200
        assert r.json()["transcribe_lyrics"] == "on"
        r = c.post("/api/settings", json={"transcribe_lyrics": "maybe"})
        assert r.status_code == 422
        assert c.get("/api/settings").json()["transcribe_lyrics"] == "on"  # unchanged


@pytest.mark.parametrize(
    ("choice", "device", "enabled"),
    [
        ("auto", "cuda", True),
        # MPS would run Whisper on the CPU (~200 s a song): off under auto.
        ("auto", "mps", False),
        ("on", "mps", True),
        ("auto", "cpu", False),
        # The GPU just failed; "auto" does not start a CPU pass after it.
        ("auto", "cpu (fallback from cuda)", False),
        ("auto", None, False),
        ("on", "cpu", True),
        ("off", "cuda", False),
    ],
)
def test_enabled_by_setting_and_device(choice, device, enabled):
    settings_mod.set_transcribe_lyrics(choice)
    assert settings_mod.transcribe_lyrics_enabled(device) is enabled


# ── gating ──


def _job_dir(tmp_path: Path, vocals: bool = True) -> Path:
    job_dir = tmp_path / "job"
    stems = job_dir / "stems"
    stems.mkdir(parents=True)
    if vocals:
        (stems / "vocals.wav").write_bytes(b"RIFF")
    return job_dir


def _job(**fields) -> Job:
    job = Job(id="abc123def456", title="Nirvana - Lithium", duration_sec=255.0)
    job.compute_device = "cuda"
    job.stem_presence = {"vocals": 79, "bass": 100}
    for key, value in fields.items():
        setattr(job, key, value)
    return job


def test_skip_reason_none_for_a_gpu_job_without_lyrics(tmp_path):
    assert tr.skip_reason(_job(), _job_dir(tmp_path)) is None


def test_skip_reason_when_the_setting_is_off(tmp_path):
    settings_mod.set_transcribe_lyrics("off")
    assert "off" in tr.skip_reason(_job(), _job_dir(tmp_path))


def test_skip_reason_auto_on_cpu(tmp_path):
    assert "off" in tr.skip_reason(_job(compute_device="cpu"), _job_dir(tmp_path))


def test_skip_reason_when_the_lookup_found_lyrics(tmp_path):
    job_dir = _job_dir(tmp_path)
    lyrics_path(job_dir).write_text("{}", encoding="utf-8")
    assert tr.skip_reason(_job(), job_dir) == "lyrics already found"
    assert tr.skip_reason(_job(has_lyrics=True), _job_dir(tmp_path / "b")) is not None


def test_skip_reason_when_the_file_carries_lyrics(tmp_path):
    job = _job(audio_tags={"artist": "Nirvana", "lyrics": "I'm so happy"})
    assert tr.skip_reason(job, _job_dir(tmp_path)) is not None


def test_skip_reason_without_vocals(tmp_path):
    assert tr.skip_reason(_job(), _job_dir(tmp_path, vocals=False)) == "no vocals stem"


def test_skip_reason_for_near_silent_vocals(tmp_path):
    job = _job(stem_presence={"vocals": 1, "bass": 100})
    assert "too quiet" in tr.skip_reason(job, _job_dir(tmp_path))


# ── the transcript as lyrics ──


def _w(text, start, end):
    return {"word": f" {text}", "start": start, "end": end, "probability": 0.9}


def _segment(words, **extra):
    seg = {
        "start": words[0]["start"],
        "end": words[-1]["end"],
        "text": "".join(w["word"] for w in words),
        "words": words,
        "no_speech_prob": 0.01,
        "avg_logprob": -0.3,
        "compression_ratio": 1.2,
    }
    seg.update(extra)
    return seg


def test_lrc_time_rounds_to_the_hundredth_without_a_60th_second():
    assert tr.lrc_time(0) == "00:00.00"
    assert tr.lrc_time(12.346) == "00:12.35"
    assert tr.lrc_time(59.999) == "01:00.00"
    assert tr.lrc_time(754.5) == "12:34.50"


def test_lrc_line_carries_word_stamps_a_pause_and_the_end():
    words = [
        {"text": "Hello", "start": 12.0, "end": 12.4},
        {"text": "big", "start": 12.45, "end": 12.6},
        {"text": "world", "start": 13.0, "end": 13.5},
    ]
    # 12.6 -> 13.0 is a 0.4 s pause: "big" ends at 12.60, then an empty piece.
    assert tr.lrc_line(words) == (
        "[00:12.00]<00:12.00>Hello <00:12.45>big <00:12.60><00:13.00>world<00:13.50>"
    )


def test_build_lyrics_shape_lines_and_stanzas():
    result = {
        "language": "en",
        "segments": [
            # One segment with a 1 s pause in it: two lines.
            _segment(
                [
                    _w("I'm", 7.9, 8.2),
                    _w("so", 8.2, 8.5),
                    _w("happy", 8.5, 9.0),
                    _w("cause", 10.0, 10.3),
                    _w("today", 10.3, 10.8),
                ]
            ),
            # Whisper's own silence rule: dropped.
            _segment(
                [_w("Thank", 11.0, 11.2), _w("you", 11.2, 11.4)],
                no_speech_prob=0.9,
                avg_logprob=-1.5,
            ),
            # A loop: dropped.
            _segment([_w("yeah", 12.0, 12.2)] * 3, compression_ratio=3.1),
            # After a long silence: a new stanza in the plain text.
            _segment([_w("I", 20.0, 20.2), _w("found", 20.2, 20.6), _w("God", 20.6, 21.0)]),
        ],
    }
    job = _job(audio_tags={"artist": "Nirvana", "title": "Lithium", "album": "Nevermind"})
    entry = tr.build_lyrics(result, job)

    assert entry["v"] == 1
    assert entry["source"] == "whisper"
    assert (entry["track"], entry["artist"], entry["album"]) == ("Lithium", "Nirvana", "Nevermind")
    assert entry["duration"] == 255.0
    assert entry["instrumental"] is False
    assert entry["others"] == [] and entry["lrclib_id"] is None
    assert entry["language"] == "en"
    assert entry["plain"] == "I'm so happy\ncause today\n\nI found God"
    synced = entry["synced"].split("\n")
    assert synced == [
        "[00:07.90]<00:07.90>I'm <00:08.20>so <00:08.50>happy<00:09.00>",
        "[00:10.00]<00:10.00>cause <00:10.30>today<00:10.80>",
        "[00:20.00]<00:20.00>I <00:20.20>found <00:20.60>God<00:21.00>",
    ]
    # What lyrics_lookup keeps: the entry survives its cleaner.
    from app.pipeline.lyrics_lookup import clean_lyrics

    assert clean_lyrics(entry)["synced"] == entry["synced"]


def test_build_lyrics_makes_stamps_monotonic():
    result = {"segments": [_segment([_w("a", 5.0, 5.4), _w("b", 4.9, 5.2), _w("c", 5.35, 5.3)])]}
    words = tr.build_lyrics(result, _job())["synced"]
    assert words == "[00:05.00]<00:05.00>a <00:05.00>b <00:05.35>c<00:05.35>"


def test_long_lines_break_at_a_clause_and_never_pass_the_hard_cap():
    words = [{"text": "word,", "start": i * 0.3, "end": i * 0.3 + 0.25} for i in range(40)]
    lines = tr.split_lines(words)
    assert len(lines) > 1
    for line in lines:
        assert len(" ".join(w["text"] for w in line)) <= tr.TRANSCRIBE_LINE_HARD_CHARS
    assert sum(len(line) for line in lines) == 40


def test_a_pause_after_a_single_word_keeps_the_line():
    # "Yeah," held for a second before the next "yeah": one line, not two.
    words = [
        {"text": "Yeah,", "start": 37.2, "end": 38.0},
        {"text": "yeah", "start": 39.1, "end": 39.6},
    ]
    assert tr.split_lines(words) == [words]


def test_build_lyrics_with_too_few_words_is_none():
    assert tr.build_lyrics({"segments": [_segment([_w("hm", 1.0, 1.2)])]}, _job()) is None
    assert tr.build_lyrics({"segments": "nope"}, _job()) is None


# ── the stage, with a stub worker ──

_ANSWER = {
    "language": "en",
    "language_probability": 0.93,
    "model": "turbo",
    "device": "cuda",
    "peak_vram_mb": 2006,
    "segments": [
        _segment([_w("I'm", 7.88, 8.1), _w("so", 8.1, 8.4), _w("happy", 8.4, 9.0)]),
    ],
}


def _stub(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "stub_worker.py"
    script.write_text("import sys, time, json\n" + body, encoding="utf-8")
    return script


def _use_stub(monkeypatch, script: Path, seen: list | None = None):
    def cmd(vocals, device):
        if seen is not None:
            seen.append(device)
        return [sys.executable, str(script)]

    monkeypatch.setattr(tr, "_spawn_worker_cmd", cmd)


def test_stage_writes_lyrics_json(tmp_path, monkeypatch):
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps(_ANSWER), encoding="utf-8")
    script = _stub(
        tmp_path,
        "print('@@PHASE@@transcribe', file=sys.stderr, flush=True)\n"
        "print(' 50%|#####     | 100/200 [00:01<00:01]', file=sys.stderr, flush=True)\n"
        f"print(open(r'{answer}', encoding='utf-8').read().strip(), flush=True)\n",
    )
    seen: list = []
    _use_stub(monkeypatch, script, seen)
    job, job_dir = _job(), _job_dir(tmp_path)

    assert tr.transcribe_lyrics(job, job_dir) is True

    assert seen == ["cuda"]
    assert job.has_lyrics is True
    saved = read_lyrics(job_dir)
    assert saved["source"] == "whisper"
    assert saved["language"] == "en"  # Whisper's detection survives write and read
    assert saved["synced"] == "[00:07.88]<00:07.88>I'm <00:08.10>so <00:08.40>happy<00:09.00>"
    assert get_proc(job.id) is None


def test_stage_runs_an_mps_job_on_the_cpu(tmp_path, monkeypatch):
    settings_mod.set_transcribe_lyrics("on")
    seen: list = []
    _use_stub(monkeypatch, _stub(tmp_path, "sys.exit(1)\n"), seen)
    tr.transcribe_lyrics(_job(compute_device="mps"), _job_dir(tmp_path))
    assert seen == ["cpu"]


def test_stage_never_spawns_when_lyrics_were_found(tmp_path, monkeypatch):
    seen: list = []
    _use_stub(monkeypatch, _stub(tmp_path, "sys.exit(1)\n"), seen)
    job_dir = _job_dir(tmp_path)
    lyrics_path(job_dir).write_text("{}", encoding="utf-8")
    assert tr.transcribe_lyrics(_job(), job_dir) is False
    assert seen == []


def test_stage_worker_failure_is_not_fatal(tmp_path, monkeypatch, caplog):
    script = _stub(tmp_path, "print('CUDA out of memory', file=sys.stderr)\nsys.exit(1)\n")
    _use_stub(monkeypatch, script)
    job, job_dir = _job(), _job_dir(tmp_path)
    with caplog.at_level("WARNING", logger="stemdeck.transcribe"):
        assert tr.transcribe_lyrics(job, job_dir) is False
    assert not lyrics_path(job_dir).exists()
    assert job.has_lyrics is False
    assert "CUDA out of memory" in caplog.text


def test_stage_ignores_output_that_is_not_one_json_line(tmp_path, monkeypatch):
    _use_stub(monkeypatch, _stub(tmp_path, "print('not json')\nprint('{broken')\n"))
    job_dir = _job_dir(tmp_path)
    assert tr.transcribe_lyrics(_job(), job_dir) is False
    assert not lyrics_path(job_dir).exists()


def test_stage_isolates_an_unexpected_error(tmp_path, monkeypatch):
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps(_ANSWER), encoding="utf-8")
    _use_stub(monkeypatch, _stub(tmp_path, f"print(open(r'{answer}').read().strip())\n"))

    def boom(result, job):
        raise KeyError("segments")

    monkeypatch.setattr(tr, "build_lyrics", boom)
    assert tr.transcribe_lyrics(_job(), _job_dir(tmp_path)) is False


def test_stage_terminates_a_stalled_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "TIMEOUT_TRANSCRIBE_STALL", 1)
    _use_stub(monkeypatch, _stub(tmp_path, "time.sleep(60)\n"))
    started = time.monotonic()
    assert tr.transcribe_lyrics(_job(), _job_dir(tmp_path)) is False
    assert time.monotonic() - started < 20


def test_stage_cancellation_reaches_the_worker(tmp_path, monkeypatch):
    script = _stub(
        tmp_path,
        "print('@@PHASE@@transcribe', file=sys.stderr, flush=True)\n"
        "for i in range(600):\n"
        "    print(f' {i % 100}%|', file=sys.stderr, flush=True)\n"
        "    time.sleep(0.1)\n",
    )
    _use_stub(monkeypatch, script)
    job, job_dir = _job(), _job_dir(tmp_path)
    threading.Timer(0.5, lambda: setattr(job, "cancel_requested", True)).start()

    started = time.monotonic()
    with pytest.raises(JobCancelled):
        tr.transcribe_lyrics(job, job_dir)
    assert time.monotonic() - started < 20
    assert get_proc(job.id) is None
    assert not lyrics_path(job_dir).exists()


def test_stage_shows_progress_while_transcribing(tmp_path, monkeypatch):
    stages: list[str] = []
    real_set = tr._set

    def spy(job, **fields):
        if "stage" in fields:
            stages.append(fields["stage"])
        real_set(job, **fields)

    monkeypatch.setattr(tr, "_set", spy)
    script = _stub(
        tmp_path,
        "print('@@PHASE@@download', file=sys.stderr, flush=True)\n"
        "print(' 40%|####', file=sys.stderr, flush=True)\n"
        "time.sleep(0.4)\n"
        "print('@@PHASE@@transcribe', file=sys.stderr, flush=True)\n"
        "print(' 70%|#######', file=sys.stderr, flush=True)\n"
        "time.sleep(0.4)\n",
    )
    _use_stub(monkeypatch, script)
    tr.transcribe_lyrics(_job(), _job_dir(tmp_path))
    assert stages[0] == "Transcribing lyrics..."
    assert "Downloading the lyrics model 40%" in stages
    assert "Transcribing lyrics 70%" in stages


@pytest.mark.parametrize("device", ["cpu", "mps"])
def test_stage_is_skipped_off_cuda_under_auto(tmp_path, monkeypatch, device):
    seen: list = []
    _use_stub(monkeypatch, _stub(tmp_path, "sys.exit(1)\n"), seen)
    assert tr.transcribe_lyrics(_job(compute_device=device), _job_dir(tmp_path)) is False
    assert seen == []


def test_models_live_beside_the_demucs_checkpoints(monkeypatch, tmp_path):
    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "torch"))
    assert tr.whisper_models_dir() == tmp_path / "torch" / "whisper"


# ── the pipeline ──


def test_runner_transcribes_after_the_lyrics_lookup(tmp_path, monkeypatch):
    from app.pipeline import runner

    order: list[str] = []

    class FakeLookup:
        def finish(self, job, job_dir, timeout):
            order.append("lyrics_finish")

    monkeypatch.setattr(runner, "_run_common", lambda job, source, job_dir: order.append("common"))
    monkeypatch.setattr(runner.IdentifyLookup, "start", classmethod(lambda cls, *a, **k: None))
    monkeypatch.setattr(
        runner.LyricsLookup, "start", classmethod(lambda cls, *a, **k: FakeLookup())
    )
    monkeypatch.setattr(
        runner, "transcribe_lyrics", lambda job, job_dir: order.append("transcribe") or False
    )
    job = _job()
    runner._run_with_band_lookup(job, tmp_path / "source.wav", tmp_path)

    assert order == ["common", "lyrics_finish", "transcribe"]
    assert "transcribe" in job.stage_timings


# ── the worker's own helpers (no model) ──


def _args(**overrides):
    import argparse

    values = {
        "device": "cuda",
        "model": "turbo",
        "fallback_model": "small",
        "min_free_mb": 3072,
        "small_min_free_mb": 1536,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.parametrize(
    ("device", "free_mb", "expected"),
    [
        ("cuda", 8000, ("cuda", "turbo")),
        ("cuda", 2000, ("cuda", "small")),
        ("cuda", 500, ("cpu", "small")),
        ("cuda", None, ("cpu", "small")),
        ("cpu", None, ("cpu", "small")),
    ],
)
def test_worker_picks_a_model_that_fits(device, free_mb, expected):
    assert worker.pick_model(_args(device=device), free_mb) == expected


def test_worker_finds_the_loudest_window_for_language_detection():
    import numpy as np

    audio = np.zeros(90 * 16000, dtype=np.float32)
    audio[50 * 16000 : 70 * 16000] = 0.5  # the singing, well past the first 30 s
    start, end = worker.loudest_window(audio)
    assert end - start == 30 * 16000
    assert start <= 50 * 16000 and end >= 70 * 16000


def test_worker_loads_a_stem_as_16k_mono(tmp_path):
    import numpy as np
    import soundfile as sf

    path = tmp_path / "vocals.wav"
    t = np.arange(44100) / 44100
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    sf.write(str(path), np.stack([tone, tone], axis=1), 44100, subtype="PCM_16")
    audio = worker.load_audio(path)
    assert audio.dtype == np.float32 and audio.ndim == 1
    assert abs(len(audio) - 16000) <= 1


def test_warmup_skips_whisper_when_transcription_would_not_run(monkeypatch):
    from app.pipeline import warmup

    settings_mod.set_transcribe_lyrics("off")
    monkeypatch.setitem(sys.modules, "whisper", None)  # importing it would fail
    warmup._warm_whisper()


def test_warmup_downloads_the_model_the_stage_would_use(monkeypatch, tmp_path):
    import types

    from app.pipeline import warmup

    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "torch"))
    monkeypatch.setattr("app.core.settings.get_demucs_device", lambda: "cuda")
    loaded = {}
    fake = types.ModuleType("whisper")
    fake.load_model = lambda name, device, download_root: loaded.update(
        name=name, device=device, root=download_root
    )
    monkeypatch.setitem(sys.modules, "whisper", fake)
    warmup._warm_whisper()
    assert loaded == {
        "name": warmup.TRANSCRIBE_MODEL_GPU,
        "device": "cpu",
        "root": str(tmp_path / "torch" / "whisper"),
    }


@pytest.mark.parametrize(
    ("language", "kept"),
    [
        ("en", True),
        ("yue", True),
        ("pt-BR", True),
        ("", False),
        ("English", False),
        ("en<script>", False),
        (7, False),
        (None, False),
    ],
)
def test_lyrics_cleaner_keeps_only_a_language_code(language, kept):
    from app.pipeline.lyrics_lookup import clean_lyrics

    entry = {"v": 1, "source": "whisper", "duration": 10.0, "plain": "la la", "language": language}
    cleaned = clean_lyrics(entry)
    assert ("language" in cleaned) is kept
    if kept:
        assert cleaned["language"] == language
