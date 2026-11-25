"""Re-extract one stem (app/pipeline/stem_reextract.py), below the routes.

The separation is a stand-in for the worker that writes the six stems a run
would write, from the song's own stems, so what the swap does with them is
checked sample for sample: the new stem in place, "other" rebuilt so the six
still add up to the song, the backing track and mix rebuilt when they hold
one of the two, the undo copy, the level rule, and the refusals that leave
every file as it was.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.core import stem_models
from app.core.config import STEM_NAMES
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import stem_reextract as rx
from tests.ffmpeg_probe import skip_without_ffmpeg

JOB = "c0ffee123456"
SR = 44100
FRAMES = SR * 2
SIX = "htdemucs_6s"


def _noise(seed: int, amp: float = 0.05) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.rint(rng.standard_normal((FRAMES, 2)) * amp * 32768).astype(np.int64)


def _write(path: Path, data: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.clip(data, -32768, 32767).astype(np.int16), SR, subtype="PCM_16")


def _read(path: Path) -> np.ndarray:
    return sf.read(str(path), dtype="int16", always_2d=True)[0].astype(np.int64)


def _song(jobs_dir: Path, *, selected=None, stems=None, amp=0.05, **fields) -> tuple[Job, dict]:
    """A finished upload with six WAV stems of noise, its kept source the
    sum of them."""
    job_dir = jobs_dir / JOB
    stems_dir = job_dir / "stems"
    values = stems or {name: _noise(i + 1, amp) for i, name in enumerate(STEM_NAMES)}
    for name, data in values.items():
        _write(stems_dir / f"{name}.wav", data)
    _write(job_dir / "source.wav", sum(values.values()))
    job = Job(
        id=JOB,
        status="done",
        title="Song",
        source_url="local:Song",
        selected_stems=list(selected or STEM_NAMES),
        separation_model=SIX,
        separation_quality="standard",
        stem_models=dict.fromkeys(stem_models.SELECTABLE_STEMS, SIX),
        stem_presence={name: 50 for name in STEM_NAMES},
        **fields,
    )
    job.stems = [{"name": n, "url": f"/api/jobs/{JOB}/stems/{n}.wav"} for n in STEM_NAMES]
    _jobs[JOB] = job
    return job, values


@pytest.fixture(autouse=True)
def _no_real_separation(monkeypatch):
    """Every test that reaches a separation sets its own stand-in; one that
    reaches the real worker by mistake fails here instead of running it."""

    def refuse(*_a, **_kw):
        raise AssertionError("a test reached the real separation")

    monkeypatch.setattr(rx, "separate", refuse)
    monkeypatch.setattr(rx, "download", refuse)


@pytest.fixture
def jobs_dir():
    from app.core import config

    yield config.JOBS_DIR
    _jobs.pop(JOB, None)


class FakeRun:
    """separate(), as the worker would answer: the run's six stems are the
    song's, but ``stem`` is ``new``, and "other" the rest of ``mix``, all
    divided by ``scale``. ``fallback`` makes it stand 6s in for the model."""

    def __init__(self, values, stem, new, *, scale=1.0, mix=None, fallback=None):
        self.values = values
        self.stem = stem
        self.new = new
        self.scale = scale
        self.mix = mix
        self.fallback = fallback
        self.plans: list[dict] = []
        self.stand_in_flags: list[bool] = []
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def __call__(self, job, source, work, quality=None, plan=None, *, stand_in=False):
        self.plans.append(dict(plan))
        self.stand_in_flags.append(stand_in)
        self.started.set()
        if self.gate is not None:
            self.gate.wait(10)
        job.stem_models = dict(plan)
        if self.fallback:
            job.stem_models[self.stem] = SIX
            job.stem_model_fallbacks = {self.stem: dict(self.fallback)}
        run = dict(self.values)
        run[self.stem] = self.new
        mix = sum(self.values.values()) if self.mix is None else self.mix
        run["other"] = mix - sum(v for n, v in run.items() if n != "other")
        out = work / "htdemucs_6s" / "source"
        for name, data in run.items():
            _write(out / f"{name}.wav", np.rint(data / self.scale))
        return out


def _sum_on_disk(stems_dir: Path) -> np.ndarray:
    return sum(_read(stems_dir / f"{n}.wav") for n in STEM_NAMES)


def _run(job, jobs_dir, stem="guitar", model=SIX):
    rx.reextract_stem(job, jobs_dir / JOB, stem, model, jobs_dir / JOB / "source.wav")
    return job.stem_reextract


# -- The swap


def test_the_stem_is_replaced_and_the_six_still_add_up(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    stems_dir = jobs_dir / JOB / "stems"
    before = _sum_on_disk(stems_dir)
    new = _noise(99)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", new))
    state = _run(job, jobs_dir)
    assert state["state"] == "done" and state["code"] is None
    assert state["used_model"] == SIX and state["gain_db"] is None
    assert np.array_equal(_read(stems_dir / "guitar.wav"), new)
    assert np.array_equal(_read(stems_dir / "other.wav"), values["other"] + values["guitar"] - new)
    # Exactly, sample for sample: a WAV song's arithmetic is in integers.
    assert np.array_equal(_sum_on_disk(stems_dir), before)
    for name in ("vocals", "drums", "bass", "piano"):
        assert np.array_equal(_read(stems_dir / f"{name}.wav"), values[name])


def test_the_undo_copy_and_the_record(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99)))
    _run(job, jobs_dir)
    entry = rx.undo_entry(job, jobs_dir / JOB, "guitar")
    assert entry is not None
    assert np.array_equal(_read(entry.file), values["guitar"])
    assert job.stem_undo["guitar"]["model"] == SIX
    assert job.to_state()["stem_undo"] == {
        "guitar": {"model": SIX, "saved_at": job.stem_undo["guitar"]["saved_at"]}
    }
    assert job.stem_revisions == {"guitar": 1, "other": 1}
    urls = {s["name"]: s["url"] for s in job.stems}
    assert urls["guitar"] == f"/api/jobs/{JOB}/stems/guitar.wav?v=1"
    assert urls["other"] == f"/api/jobs/{JOB}/stems/other.wav?v=1"
    assert urls["drums"] == f"/api/jobs/{JOB}/stems/drums.wav"
    assert (jobs_dir / JOB / "stems" / "peaks.json").is_file()
    assert set(job.stem_presence) == set(STEM_NAMES)


def test_undo_puts_the_old_bytes_back(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    stems_dir = jobs_dir / JOB / "stems"
    old_guitar = (stems_dir / "guitar.wav").read_bytes()
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99)))
    _run(job, jobs_dir, model=SIX)
    job.stem_models = {**job.stem_models, "guitar": SIX}
    rx.undo_stem(job, jobs_dir / JOB, "guitar")
    assert (stems_dir / "guitar.wav").read_bytes() == old_guitar
    assert np.array_equal(_read(stems_dir / "other.wav"), values["other"])
    assert job.stem_undo is None
    assert not (jobs_dir / JOB / rx.UNDO_DIRNAME).exists()
    assert job.stem_revisions == {"guitar": 2, "other": 2}
    assert job.stem_reextract["action"] == "undo" and job.stem_reextract["state"] == "done"


def test_undo_after_another_stem_changed_keeps_both_changes_adding_up(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    stems_dir = jobs_dir / JOB / "stems"
    before = _sum_on_disk(stems_dir)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99)))
    _run(job, jobs_dir, "guitar")
    now = {n: _read(stems_dir / f"{n}.wav") for n in STEM_NAMES}
    new_bass = _noise(77)
    monkeypatch.setattr(rx, "separate", FakeRun(now, "bass", new_bass))
    _run(job, jobs_dir, "bass")
    rx.undo_stem(job, jobs_dir / JOB, "guitar")
    assert np.array_equal(_read(stems_dir / "guitar.wav"), values["guitar"])
    assert np.array_equal(_read(stems_dir / "bass.wav"), new_bass)
    assert np.array_equal(_sum_on_disk(stems_dir), before)
    assert set(job.stem_undo) == {"bass"}


def test_re_extracting_again_replaces_the_undo_copy(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    stems_dir = jobs_dir / JOB / "stems"
    first = _noise(99)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", first))
    _run(job, jobs_dir)
    old_dir = job.stem_undo["guitar"]["dir"]
    now = {n: _read(stems_dir / f"{n}.wav") for n in STEM_NAMES}
    monkeypatch.setattr(rx, "separate", FakeRun(now, "guitar", _noise(98)))
    _run(job, jobs_dir)
    entry = rx.undo_entry(job, jobs_dir / JOB, "guitar")
    assert np.array_equal(_read(entry.file), first)
    assert not (jobs_dir / JOB / rx.UNDO_DIRNAME / old_dir).exists()


def test_keep_drops_the_undo_copy(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99)))
    _run(job, jobs_dir)
    rx.keep_stem(job, jobs_dir / JOB, "guitar")
    assert job.stem_undo is None
    assert not (jobs_dir / JOB / rx.UNDO_DIRNAME).exists()
    with pytest.raises(rx.ReextractRefused) as e:
        rx.keep_stem(job, jobs_dir / JOB, "guitar")
    assert e.value.code == "no_undo"


def test_another_scale_in_the_new_run_is_measured_and_undone(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    stems_dir = jobs_dir / JOB / "stems"
    before = _sum_on_disk(stems_dir)
    new = _noise(99)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", new, scale=1.25))
    assert _run(job, jobs_dir)["state"] == "done"
    got = _read(stems_dir / "guitar.wav")
    assert np.max(np.abs(got - new)) <= 2  # the run's own rounding, times 1.25
    assert np.array_equal(_sum_on_disk(stems_dir), before)


def test_the_backing_track_and_mix_are_rebuilt_when_they_hold_one_of_the_two(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir, selected=["vocals", "drums"])
    stems_dir = jobs_dir / JOB / "stems"
    unselected = [n for n in STEM_NAMES if n not in ("vocals", "drums")]
    _write(stems_dir / "original.wav", sum(values[n] for n in unselected))
    _write(stems_dir / "mix.wav", values["vocals"] + values["drums"])
    original_before = (stems_dir / "original.wav").read_bytes()
    # Guitar and other are both in the backing track: it does not change.
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99)))
    _run(job, jobs_dir, "guitar")
    assert (stems_dir / "original.wav").read_bytes() == original_before
    now = {n: _read(stems_dir / f"{n}.wav") for n in STEM_NAMES}
    # Vocals is in the mix, other in the backing track: both change.
    monkeypatch.setattr(rx, "separate", FakeRun(now, "vocals", _noise(55)))
    _run(job, jobs_dir, "vocals")
    after = {n: _read(stems_dir / f"{n}.wav") for n in STEM_NAMES}
    assert np.array_equal(_read(stems_dir / "original.wav"), sum(after[n] for n in unselected))
    assert np.array_equal(_read(stems_dir / "mix.wav"), after["vocals"] + after["drums"])
    assert job.stem_revisions["mix"] == 1 and job.stem_revisions["original"] == 1


def test_a_stem_that_would_clip_turns_every_stem_down_by_the_same_factor(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir, amp=0.003)
    stems_dir = jobs_dir / JOB / "stems"
    before = _sum_on_disk(stems_dir)
    burst = np.zeros((FRAMES, 2), dtype=np.int64)
    burst[1000:3000] = 32200
    # The run puts a loud burst in guitar and takes it out of drums, so its
    # sum is still the song's; the swap then needs "other" to take it out.
    run_values = dict(values)
    run_values["drums"] = values["drums"] - burst
    fake = FakeRun(run_values, "guitar", values["guitar"] + burst, mix=sum(values.values()))
    monkeypatch.setattr(rx, "separate", fake)
    state = _run(job, jobs_dir)
    assert state["state"] == "done"
    assert state["gain_db"] is not None and -0.2 < state["gain_db"] < 0
    gain = job.stem_gain
    assert 0.98 < gain < 1.0
    after = _sum_on_disk(stems_dir)
    assert np.max(np.abs(after - np.rint(before * gain))) <= 6
    for name in STEM_NAMES:
        assert np.max(np.abs(_read(stems_dir / f"{name}.wav"))) <= rx._LIMIT
    assert set(job.stem_revisions) == set(STEM_NAMES)
    # The undo copy was saved at the level before: it comes back at this one.
    rx.undo_stem(job, jobs_dir / JOB, "guitar")
    assert np.max(np.abs(_read(stems_dir / "guitar.wav") - np.rint(values["guitar"] * gain))) <= 1
    assert np.max(np.abs(_sum_on_disk(stems_dir) - np.rint(before * gain))) <= 8


def test_vocals_take_their_lead_and_backing_into_the_undo_copy(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir, vocal_split="done")
    stems_dir = jobs_dir / JOB / "stems"
    _write(stems_dir / "lead_vocals.wav", values["vocals"] // 2)
    _write(stems_dir / "backing_vocals.wav", values["vocals"] - values["vocals"] // 2)
    (stems_dir / "vocal_envelope.json").write_text("{}")
    job.stems += [
        {"name": "lead_vocals", "url": "x"},
        {"name": "backing_vocals", "url": "y"},
    ]
    lead = (stems_dir / "lead_vocals.wav").read_bytes()
    monkeypatch.setattr(rx, "separate", FakeRun(values, "vocals", _noise(42)))
    _run(job, jobs_dir, "vocals")
    assert not (stems_dir / "lead_vocals.wav").exists()
    assert not (stems_dir / "vocal_envelope.json").exists()
    assert job.vocal_split == "none"
    assert {s["name"] for s in job.stems} == set(STEM_NAMES)
    rx.undo_stem(job, jobs_dir / JOB, "vocals")
    assert (stems_dir / "lead_vocals.wav").read_bytes() == lead
    assert job.vocal_split == "done"
    assert {"lead_vocals", "backing_vocals"} <= {s["name"] for s in job.stems}


# -- What leaves the song as it was


def _files(stems_dir: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(stems_dir.iterdir()) if p.is_file()}


def test_audio_that_is_not_the_song_any_more_is_refused(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    stems_dir = jobs_dir / JOB / "stems"
    before = _files(stems_dir)
    other_song = sum(_noise(100 + i) for i in range(6))
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99), mix=other_song))
    state = _run(job, jobs_dir)
    assert (state["state"], state["code"]) == ("error", "source_changed")
    assert _files(stems_dir) == before
    assert job.stem_undo is None and job.stem_revisions is None


def test_a_shifted_run_is_refused(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    shifted = {n: np.roll(v, 3, axis=0) for n, v in values.items()}
    monkeypatch.setattr(rx, "separate", FakeRun(shifted, "guitar", _noise(99)))
    assert _run(job, jobs_dir)["code"] == "source_changed"


def test_a_cancel_during_the_separation_changes_nothing(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    stems_dir = jobs_dir / JOB / "stems"
    before = _files(stems_dir)
    fake = FakeRun(values, "guitar", _noise(99))
    fake.gate = threading.Event()
    monkeypatch.setattr(rx, "separate", fake)
    worker = threading.Thread(target=_run, args=(job, jobs_dir))
    worker.start()
    assert fake.started.wait(5)
    assert rx.cancel(JOB)
    fake.gate.set()
    worker.join(10)
    assert job.stem_reextract["state"] == "cancelled"
    assert _files(stems_dir) == before
    assert not rx.cancel(JOB)
    assert not any(p.name.startswith(".fill-") for p in (jobs_dir / JOB).iterdir())


def test_a_failing_separation_is_an_error_and_changes_nothing(jobs_dir, monkeypatch):
    job, _values = _song(jobs_dir)
    before = _files(jobs_dir / JOB / "stems")

    def broken(*a, **kw):
        raise RuntimeError("worker died")

    monkeypatch.setattr(rx, "separate", broken)
    state = _run(job, jobs_dir)
    assert (state["state"], state["code"]) == ("error", "failed")
    assert _files(jobs_dir / JOB / "stems") == before


def test_a_stood_in_model_is_recorded(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    user = "user_0123456789ab"
    monkeypatch.setattr(stem_models, "user_model_entry", lambda m: {"stems": ["guitar"]})
    monkeypatch.setattr(stem_models, "user_model_usable", lambda m: True)
    monkeypatch.setattr(
        stem_models,
        "choices_for",
        lambda s: (*stem_models.CHOICES.get(s, ()), *((user,) if s == "guitar" else ())),
    )
    record = {"model": user, "name": "Mine", "code": "load_failed"}
    fake = FakeRun(values, "guitar", _noise(99), fallback=record)
    monkeypatch.setattr(rx, "separate", fake)
    state = _run(job, jobs_dir, "guitar", user)
    assert fake.stand_in_flags == [True]
    assert fake.plans[0]["guitar"] == user
    assert state["used_model"] == SIX and state["fallback"] == record
    assert job.stem_models["guitar"] == SIX
    assert job.stem_model_fallbacks == {"guitar": record}


def test_a_link_song_is_fetched_again(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    job.source_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    fetched: list[str] = []

    def fetch(stand_in, url, work):
        fetched.append(url)
        dest = work / "source.wav"
        _write(dest, sum(values.values()))
        return dest

    monkeypatch.setattr(rx, "download", fetch)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99)))
    rx.reextract_stem(job, jobs_dir / JOB, "guitar", SIX, None)
    assert fetched == [job.source_url]
    assert job.stem_reextract["state"] == "done"


def test_no_source_is_refused(jobs_dir):
    job, _values = _song(jobs_dir)
    rx.reextract_stem(job, jobs_dir / JOB, "guitar", SIX, None)
    assert job.stem_reextract["code"] == "no_source"


def test_a_song_missing_other_is_refused(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    (jobs_dir / JOB / "stems" / "other.wav").unlink()
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99)))
    assert _run(job, jobs_dir)["code"] == "stems_missing"


def test_a_damaged_undo_record_reads_as_none(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99)))
    _run(job, jobs_dir)
    good = dict(job.stem_undo["guitar"])
    for bad in (
        {**good, "dir": "../../etc"},
        {**good, "file": "../guitar.wav"},
        {**good, "file": "drums.wav"},
        {**good, "dir": "drums-0000abcd"},
        {**good, "extras": ["../../registry.json"]},
        "nonsense",
    ):
        job.stem_undo = {"guitar": bad}
        assert rx.undo_entry(job, jobs_dir / JOB, "guitar") is None, bad


# -- MP3 songs


def _encode(src: Path, dest: Path) -> None:
    import subprocess

    from app.core.config import ffmpeg_executable

    subprocess.run(
        [ffmpeg_executable(), "-v", "error", "-y", "-i", str(src), "-c:a", "libmp3lame",
         "-b:a", "192k", "-write_xing", "1", str(dest)],
        check=True,
    )  # fmt: skip


def _tone(freq: float, amp: float = 0.05) -> np.ndarray:
    t = np.arange(FRAMES) / SR
    wave = np.sin(2 * np.pi * freq * t) * amp * 32768
    return np.rint(np.stack([wave, wave], axis=1)).astype(np.int64)


def test_an_mp3_song_keeps_its_stems_as_mp3(jobs_dir, monkeypatch):
    skip_without_ffmpeg()
    tones = {n: _tone(110 * (i + 1)) for i, n in enumerate(STEM_NAMES)}
    job, values = _song(jobs_dir, stems=tones, stem_format="mp3", stem_mp3_kbps=192)
    stems_dir = jobs_dir / JOB / "stems"
    for name in STEM_NAMES:
        _encode(stems_dir / f"{name}.wav", stems_dir / f"{name}.mp3")
        (stems_dir / f"{name}.wav").unlink()
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _tone(1000)))
    state = _run(job, jobs_dir)
    assert state["state"] == "done", state
    assert not list(stems_dir.glob("*.wav"))
    assert (stems_dir / "guitar.mp3").is_file()
    entry = rx.undo_entry(job, jobs_dir / JOB, "guitar")
    assert entry.file.suffix == ".mp3"
    got = sf.read(str(stems_dir / "guitar.mp3"), dtype="float32")[0] * 32768
    want = _tone(1000)
    # A lossy encode moves each sample a little, by an amount that depends on the
    # ffmpeg build, so the check is that the same tone came back, not that the
    # samples match to a fixed percentage.
    assert np.corrcoef(got.astype(float).ravel(), want.astype(float).ravel())[0, 1] > 0.99
    assert job.stems[STEM_NAMES.index("guitar")]["url"].endswith("guitar.mp3?v=1")
    rx.undo_stem(job, jobs_dir / JOB, "guitar")
    assert not list(stems_dir.glob("*.wav"))


# -- The plan


def test_the_run_plan_has_the_chosen_model_and_as_few_passes_as_it_can(monkeypatch):
    monkeypatch.setattr(stem_models, "model_available", lambda m: True)
    all_6s = dict.fromkeys(stem_models.SELECTABLE_STEMS, SIX)
    plan = rx.run_plan(all_6s, "drums", "htdemucs_ft")
    assert plan == {**all_6s, "drums": "htdemucs_ft", "bass": "htdemucs_ft"}
    plan = rx.run_plan(all_6s, "vocals", "kim_mel_roformer")
    assert plan == {**all_6s, "vocals": "kim_mel_roformer"}
    # A Demucs pass hears the mix minus the song's RoFormer stems, as in an
    # import with this choice.
    with_kim = {**all_6s, "vocals": "kim_mel_roformer"}
    plan = rx.run_plan(with_kim, "drums", "htdemucs_ft")
    assert plan["vocals"] == "kim_mel_roformer" and plan["drums"] == "htdemucs_ft"
    # One that cannot run here is not kept.
    monkeypatch.setattr(stem_models, "model_available", lambda m: m != "kim_mel_roformer")
    assert rx.run_plan(with_kim, "drums", "htdemucs_ft")["vocals"] == SIX


def test_models_are_checked(monkeypatch):
    for stem, model, code in (
        ("other", SIX, "stem_not_selectable"),
        ("lead_vocals", SIX, "stem_not_selectable"),
        ("guitar", "kim_mel_roformer", "model_not_allowed"),
        ("guitar", "nope", "model_not_allowed"),
        ("guitar", "user_0123456789ab", "model_removed"),
    ):
        monkeypatch.setattr(stem_models, "user_model_entry", lambda m: None)
        with pytest.raises(rx.ReextractRefused) as e:
            rx.check_model(stem, model)
        assert e.value.code == code, (stem, model)
    monkeypatch.setattr(stem_models, "model_available", lambda m: False)
    with pytest.raises(rx.ReextractRefused) as e:
        rx.check_model("vocals", "kim_mel_roformer")
    assert e.value.code == "model_unavailable"
    monkeypatch.setattr(stem_models, "user_model_entry", lambda m: {"stems": ["bass"]})
    monkeypatch.setattr(stem_models, "user_model_usable", lambda m: False)
    for stem, code in (("guitar", "model_not_allowed"), ("bass", "model_unavailable")):
        with pytest.raises(rx.ReextractRefused) as e:
            rx.check_model(stem, "user_0123456789ab")
        assert e.value.code == code


# -- A stem the studio holds open (Windows)


def test_a_stem_held_open_for_a_moment_is_replaced_once_it_is_free(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    new = _noise(99)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", new))
    real = rx.os.replace
    refusals = [3]

    def held(src, dest):
        if refusals[0] and Path(dest).name == "guitar.wav":
            refusals[0] -= 1
            raise PermissionError(32, "in use")
        real(src, dest)

    monkeypatch.setattr(rx.os, "replace", held)
    monkeypatch.setattr(rx, "_HELD_RETRY_STEP", 0.01)
    assert _run(job, jobs_dir)["state"] == "done"
    assert np.array_equal(_read(jobs_dir / JOB / "stems" / "guitar.wav"), new)


def test_a_stem_that_stays_held_leaves_the_song_as_it_was(jobs_dir, monkeypatch):
    job, values = _song(jobs_dir)
    stems_dir = jobs_dir / JOB / "stems"
    before = _files(stems_dir)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99)))

    def held(src, dest):
        raise PermissionError(32, "in use")

    monkeypatch.setattr(rx.os, "replace", held)
    monkeypatch.setattr(rx, "_HELD_RETRY_SEC", 0.05)
    monkeypatch.setattr(rx, "_HELD_RETRY_STEP", 0.01)
    state = _run(job, jobs_dir)
    assert (state["state"], state["code"]) == ("error", "failed")
    assert _files(stems_dir) == before
    assert job.stem_undo is None
    assert not (jobs_dir / JOB / rx.UNDO_DIRNAME).exists() or not any(
        (jobs_dir / JOB / rx.UNDO_DIRNAME).iterdir()
    )
