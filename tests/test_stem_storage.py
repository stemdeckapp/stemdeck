"""Stem storage format (#783): WAV or MP3 stems, one resolver, sample accuracy.

- The setting: MP3 128 by default, env seeds it, a saved choice wins, a
  whitelist guards both values, a non-object body changes nothing.
- The storage stage: MP3 stems replace the WAVs only after every one decoded
  to the WAV's exact length and lined up; every failure keeps the WAVs and
  records the song as WAV; a cancel cleans up and raises; WAV touches nothing.
- Sample accuracy: an MP3 stem decodes to the WAV's samples in length and
  position (lag 0 by cross-correlation) in ffmpeg and libsndfile, at every
  offered bitrate, and an export from MP3 stems lines up with one from WAVs.
- Readers: the stem URL serves the right file and type for either format, the
  zip, the region, the mixdown, the backing, the take frame count, recovery
  and the sweep all find MP3 stems, and a WAV song with a cached download MP3
  still reads its WAV.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pytest
import soundfile as sf

from app.api import stems as stems_mod
from app.core import settings
from app.core.models import Job, JobCancelled
from app.core.registry import _jobs
from app.core.stem_files import stem_names_on_disk, stem_path, stem_url
from app.pipeline import stem_storage
from tests.ffmpeg_probe import skip_without_ffmpeg

SR = 44100
# The signal every alignment test uses: broadband noise bursts on a tone, with
# hard clicks once a second, so the cross-correlation has one sharp peak.
SECONDS = 6.0


def _signal(seed: int = 7, seconds: float = SECONDS) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    t = np.arange(n) / SR
    env = np.where((t % 0.5) < 0.05, 1.0, 0.25)[:, None]
    x = rng.standard_normal((n, 2)) * 0.08 * env + 0.2 * np.sin(2 * np.pi * 220 * t)[:, None]
    for k in range(1, int(seconds)):
        x[k * SR, :] = 0.9
    return x.clip(-1, 1)


def _lag(a: np.ndarray, b: np.ndarray) -> int:
    """Lag of b against a in samples (positive: b is late), by FFT xcorr."""
    m = min(len(a), len(b))
    a, b = a[:m], b[:m]
    nfft = 1 << (2 * m).bit_length()
    c = np.fft.irfft(np.fft.rfft(b, nfft) * np.conj(np.fft.rfft(a, nfft)), nfft)
    c = np.concatenate([c[-(m - 1) :], c[:m]])
    return int(np.argmax(c)) - (m - 1)


def _ffmpeg_decode(path: Path) -> np.ndarray:
    import subprocess

    from app.core.config import ffmpeg_executable

    out = subprocess.run(
        [ffmpeg_executable(), "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-"],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(out, dtype=np.float32).reshape(-1, 2)


def _job(jobs_dir: Path, job_id: str, names=("vocals", "drums", "bass"), seconds=SECONDS) -> Job:
    job = Job(id=job_id, status="done", title="Storage", duration_sec=seconds)
    stems = jobs_dir / job_id / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(names):
        sf.write(stems / f"{name}.wav", _signal(seed=i + 1, seconds=seconds), SR, subtype="PCM_16")
        job.stems.append({"name": name, "url": f"/api/jobs/{job_id}/stems/{name}.wav"})
    _jobs[job.id] = job
    return job


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", tmp_path / "cache" / "click")
    monkeypatch.setattr(stems_mod, "_MASTER_PLAN_DIR", tmp_path / "cache" / "master")
    monkeypatch.delenv("STEMDECK_STEM_FORMAT", raising=False)
    monkeypatch.delenv("STEMDECK_STEM_MP3_KBPS", raising=False)
    from app.core import config

    return config.JOBS_DIR


@pytest.fixture
def client(jobs_dir):
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _store(job: Job, jobs_dir: Path, fmt: str = "mp3", kbps: int = 128) -> Path:
    settings.set_stem_storage(fmt, kbps)
    stem_storage.store_stems(job, jobs_dir / job.id)
    return jobs_dir / job.id / "stems"


# ── The setting ──


def test_default_is_mp3_128_with_nothing_saved(jobs_dir):
    assert settings.get_stem_format() == "mp3"
    assert settings.get_stem_mp3_kbps() == 128


def test_env_seeds_the_default_and_a_saved_choice_wins(jobs_dir, monkeypatch):
    monkeypatch.setenv("STEMDECK_STEM_FORMAT", "wav")
    monkeypatch.setenv("STEMDECK_STEM_MP3_KBPS", "256")
    assert (settings.get_stem_format(), settings.get_stem_mp3_kbps()) == ("wav", 256)
    settings.set_stem_storage("mp3", 192)
    assert (settings.get_stem_format(), settings.get_stem_mp3_kbps()) == ("mp3", 192)


@pytest.mark.parametrize("fmt", ["flac", "MP4", "", "../wav"])
def test_env_outside_the_whitelist_falls_back_to_the_default(jobs_dir, monkeypatch, fmt):
    monkeypatch.setenv("STEMDECK_STEM_FORMAT", fmt)
    monkeypatch.setenv("STEMDECK_STEM_MP3_KBPS", "320")
    assert (settings.get_stem_format(), settings.get_stem_mp3_kbps()) == ("mp3", 128)


async def test_settings_endpoint_reads_and_saves_both(client):
    async with client as c:
        r = await c.get("/api/settings")
        assert (r.json()["stem_format"], r.json()["stem_mp3_kbps"]) == ("mp3", 128)
        r = await c.post("/api/settings", json={"stem_format": "wav"})
        assert r.status_code == 200
        assert r.json()["stem_format"] == "wav"
        r = await c.post("/api/settings", json={"stem_format": "mp3", "stem_mp3_kbps": 256})
        assert (r.json()["stem_format"], r.json()["stem_mp3_kbps"]) == ("mp3", 256)


@pytest.mark.parametrize(
    "body",
    [
        {"stem_format": "flac"},
        {"stem_format": 1},
        {"stem_mp3_kbps": 320},
        {"stem_mp3_kbps": "128"},
        {"stem_mp3_kbps": True},
        {"stem_format": "wav", "stem_mp3_kbps": 64},
    ],
)
async def test_settings_endpoint_rejects_values_outside_the_whitelist(client, body):
    async with client as c:
        r = await c.post("/api/settings", json=body)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "must be one of" in detail
        # Never echoes what was sent.
        assert "flac" not in detail and "320" not in detail and "64" not in detail
        # One bad value changes nothing, not even the good one beside it.
        r = await c.get("/api/settings")
        assert (r.json()["stem_format"], r.json()["stem_mp3_kbps"]) == ("mp3", 128)


@pytest.mark.parametrize("raw", ["[1, 2]", '"mp3"', "null", "not json"])
async def test_settings_endpoint_ignores_a_body_that_is_not_an_object(client, raw):
    async with client as c:
        r = await c.post("/api/settings", content=raw, headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        assert (r.json()["stem_format"], r.json()["stem_mp3_kbps"]) == ("mp3", 128)


def test_the_stage_reads_the_setting_when_the_job_finishes(jobs_dir):
    """No restart: a change applies to the next song that finishes."""
    first = _job(jobs_dir, "a1b2c3d4e501", names=("vocals",))
    _store(first, jobs_dir, "wav")
    second = _job(jobs_dir, "a1b2c3d4e502", names=("vocals",))
    settings.set_stem_storage("mp3", 192)
    stem_storage.store_stems(second, jobs_dir / second.id)
    assert (first.stem_format, first.stem_mp3_kbps) == ("wav", None)
    assert (second.stem_format, second.stem_mp3_kbps) == ("mp3", 192)


# ── The resolver ──


def test_resolver_prefers_wav_then_mp3_then_the_missing_wav_path(tmp_path):
    (tmp_path / "vocals.wav").write_bytes(b"w")
    (tmp_path / "vocals.mp3").write_bytes(b"m")  # a WAV song's download cache
    (tmp_path / "drums.mp3").write_bytes(b"m")
    (tmp_path / ".bass.abc.tmp.mp3").write_bytes(b"t")  # a storage temp file
    assert stem_path(tmp_path, "vocals").name == "vocals.wav"
    assert stem_path(tmp_path, "drums").name == "drums.mp3"
    assert stem_path(tmp_path, "bass") == tmp_path / "bass.wav"
    assert not stem_path(tmp_path, "bass").is_file()
    assert stem_names_on_disk(tmp_path) == ["drums", "vocals"]
    assert stem_url("abcdef123456", tmp_path, "drums") == "/api/jobs/abcdef123456/stems/drums.mp3"
    assert stem_url("abcdef123456", tmp_path, "vocals") == "/api/jobs/abcdef123456/stems/vocals.wav"


# ── The stage: success ──


@pytest.mark.parametrize("kbps", [128, 192, 256])
def test_mp3_storage_is_sample_aligned_in_ffmpeg_and_libsndfile(jobs_dir, kbps):
    """The alignment guard. Fails if encoder delay or padding ever leaks into
    a decoded stem: length must match exactly, and the cross-correlation
    peak must sit at lag 0, in both server-side decoders."""
    skip_without_ffmpeg()
    job = _job(jobs_dir, "a1b2c3d4e5f6", names=("vocals",))
    wav = jobs_dir / job.id / "stems" / "vocals.wav"
    ref, _ = sf.read(wav)
    stems = _store(job, jobs_dir, "mp3", kbps)
    mp3 = stems / "vocals.mp3"
    assert mp3.is_file() and not wav.exists()
    via_ffmpeg = _ffmpeg_decode(mp3)
    via_sndfile, rate = sf.read(mp3)
    assert rate == SR
    for decoded in (via_ffmpeg, via_sndfile):
        assert len(decoded) == len(ref)
        assert _lag(ref[:, 0], decoded[:, 0]) == 0
        assert _lag(ref[:, 1], decoded[:, 1]) == 0


def test_mp3_storage_replaces_every_stem_and_records_the_job(jobs_dir):
    skip_without_ffmpeg()
    job = _job(jobs_dir, "a1b2c3d4e5f7", names=("vocals", "drums", "bass", "original", "mix"))
    job.mix_url = f"/api/jobs/{job.id}/stems/mix.wav"
    before = sum(p.stat().st_size for p in (jobs_dir / job.id / "stems").glob("*.wav"))
    stems = _store(job, jobs_dir, "mp3", 128)
    assert sorted(p.name for p in stems.iterdir()) == sorted(
        f"{n}.mp3" for n in ("vocals", "drums", "bass", "original", "mix")
    )
    after = sum(p.stat().st_size for p in stems.glob("*.mp3"))
    assert after < before / 5
    assert (job.stem_format, job.stem_mp3_kbps) == ("mp3", 128)
    assert all(s["url"].endswith(".mp3") for s in job.stems)
    assert job.mix_url == f"/api/jobs/{job.id}/stems/mix.mp3"


def test_wav_storage_touches_nothing(jobs_dir):
    job = _job(jobs_dir, "a1b2c3d4e5f8", names=("vocals", "drums"))
    stems = jobs_dir / job.id / "stems"
    snapshot = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in stems.iterdir()}
    urls = [dict(s) for s in job.stems]
    _store(job, jobs_dir, "wav")
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in stems.iterdir()} == snapshot
    assert job.stems == urls
    assert (job.stem_format, job.stem_mp3_kbps) == ("wav", None)


def test_nothing_final_exists_until_every_stem_verified(jobs_dir, monkeypatch):
    """Atomic: no <name>.mp3 appears, and no WAV goes, before the last
    stem passed its check."""
    skip_without_ffmpeg()
    job = _job(jobs_dir, "a1b2c3d4e5f9", names=("vocals", "drums", "bass"))
    stems = jobs_dir / job.id / "stems"
    real = stem_storage.verify_mp3
    seen: list[tuple[list[str], list[str]]] = []

    def watching(job_, wav, mp3):
        seen.append(
            (
                sorted(p.name for p in stems.glob("*.mp3") if not p.name.startswith(".")),
                sorted(p.name for p in stems.glob("*.wav")),
            )
        )
        real(job_, wav, mp3)

    monkeypatch.setattr(stem_storage, "verify_mp3", watching)
    _store(job, jobs_dir)
    assert len(seen) == 3
    for finals, wavs in seen:
        assert finals == []
        assert wavs == ["bass.wav", "drums.wav", "vocals.wav"]


def test_transcript_and_envelope_kept_from_the_wav_stay_valid(jobs_dir):
    skip_without_ffmpeg()
    from app.pipeline.lyrics_retime import read_transcript, save_transcript

    job = _job(jobs_dir, "a1b2c3d4e5fa", names=("vocals",))
    job_dir = jobs_dir / job.id
    save_transcript(job_dir, words=[{"start": 0.5, "end": 1.0, "text": "la"}], language="en")
    envelope = job_dir / "stems" / "vocal_envelope.json"
    envelope.write_text(json.dumps({"hop": 0.04, "db": [-20]}), encoding="utf-8")
    time.sleep(0.01)
    _store(job, jobs_dir)
    assert read_transcript(job_dir) is not None
    vocals = job_dir / "stems" / "vocals.mp3"
    assert envelope.stat().st_mtime_ns >= vocals.stat().st_mtime_ns


# ── The stage: every failure keeps the WAVs ──


def _stub(monkeypatch, code: str) -> None:
    """Replace the encoder with a Python one-liner; argv[1] is the output."""
    monkeypatch.setattr(
        stem_storage,
        "_encode_cmd",
        lambda src, dest, kbps: [sys.executable, "-c", code, str(dest), str(src)],
    )


def _assert_kept_as_wav(job: Job, jobs_dir: Path, names) -> None:
    stems = jobs_dir / job.id / "stems"
    assert sorted(p.name for p in stems.iterdir()) == sorted(f"{n}.wav" for n in names)
    assert (job.stem_format, job.stem_mp3_kbps) == ("wav", None)
    assert all(s["url"].endswith(".wav") for s in job.stems)


def test_ffmpeg_without_libmp3lame_keeps_the_wavs(jobs_dir, monkeypatch):
    skip_without_ffmpeg()
    real = stem_storage._encode_cmd

    def no_lame(src, dest, kbps):
        cmd = real(src, dest, kbps)
        cmd[cmd.index("libmp3lame")] = "libmp3lame_not_built_in"
        return cmd

    monkeypatch.setattr(stem_storage, "_encode_cmd", no_lame)
    job = _job(jobs_dir, "b1b2c3d4e501", names=("vocals", "drums"))
    _store(job, jobs_dir)
    _assert_kept_as_wav(job, jobs_dir, ("vocals", "drums"))


def test_a_full_disk_keeps_the_wavs(jobs_dir, monkeypatch):
    _stub(
        monkeypatch,
        "import sys; open(sys.argv[1], 'wb').write(b'\\xff\\xfb' * 100); "
        "sys.stderr.write('Error writing trailer: No space left on device\\n'); sys.exit(1)",
    )
    job = _job(jobs_dir, "b1b2c3d4e502", names=("vocals", "drums"))
    _store(job, jobs_dir)
    _assert_kept_as_wav(job, jobs_dir, ("vocals", "drums"))


def test_a_corrupt_output_keeps_the_wavs(jobs_dir, monkeypatch):
    skip_without_ffmpeg()
    _stub(monkeypatch, "import sys; open(sys.argv[1], 'wb').write(b'not an mp3 at all' * 999)")
    job = _job(jobs_dir, "b1b2c3d4e503", names=("vocals",))
    _store(job, jobs_dir)
    _assert_kept_as_wav(job, jobs_dir, ("vocals",))


def test_a_short_output_keeps_the_wavs(jobs_dir, monkeypatch):
    """Decodes fine, one second too short: the length check refuses it."""
    skip_without_ffmpeg()
    real = stem_storage._encode_cmd
    monkeypatch.setattr(
        stem_storage,
        "_encode_cmd",
        lambda src, dest, kbps: [*real(src, dest, kbps)[:-1], "-t", "5", str(dest)],
    )
    job = _job(jobs_dir, "b1b2c3d4e504", names=("vocals",))
    _store(job, jobs_dir)
    _assert_kept_as_wav(job, jobs_dir, ("vocals",))


@pytest.mark.parametrize("shift", [576, 1105, 2000])
def test_a_shifted_output_keeps_the_wavs(jobs_dir, monkeypatch, shift):
    """Right length, audio late by an encoder/decoder delay (what a decoder
    that ignored the LAME header would produce): the alignment check
    refuses it."""
    skip_without_ffmpeg()
    job = _job(jobs_dir, "b1b2c3d4e505", names=("vocals",))
    frames = int(SR * SECONDS)
    real = stem_storage._encode_cmd

    def shifted(src, dest, kbps):
        cmd = real(src, dest, kbps)
        at = cmd.index("-c:a")
        return [
            *cmd[:at],
            "-af",
            f"adelay=delays={shift}S:all=1,atrim=end_sample={frames}",
            *cmd[at:],
        ]

    monkeypatch.setattr(stem_storage, "_encode_cmd", shifted)
    _store(job, jobs_dir)
    _assert_kept_as_wav(job, jobs_dir, ("vocals",))


def test_a_failed_move_keeps_the_wavs_and_leaves_no_mp3(jobs_dir, monkeypatch):
    skip_without_ffmpeg()
    job = _job(jobs_dir, "b1b2c3d4e506", names=("vocals", "drums", "bass"))
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        real(src, dst)

    monkeypatch.setattr(stem_storage.os, "replace", flaky)
    _store(job, jobs_dir)
    _assert_kept_as_wav(job, jobs_dir, ("vocals", "drums", "bass"))


def test_a_stalled_encoder_is_stopped_and_the_wavs_kept(jobs_dir, monkeypatch):
    monkeypatch.setattr(stem_storage, "TIMEOUT_STEM_TRANSCODE_STALL", 1)
    _stub(monkeypatch, "import time; time.sleep(60)")
    job = _job(jobs_dir, "b1b2c3d4e507", names=("vocals",))
    started = time.monotonic()
    _store(job, jobs_dir)
    assert time.monotonic() - started < 20
    _assert_kept_as_wav(job, jobs_dir, ("vocals",))


def test_a_cancel_mid_encode_stops_it_cleans_up_and_raises(jobs_dir, monkeypatch):
    _stub(
        monkeypatch,
        "import sys, time\n"
        "open(sys.argv[1], 'wb').write(b'partial')\n"
        "for _ in range(600):\n"
        "    print('progress=continue', flush=True); time.sleep(0.1)\n",
    )
    job = _job(jobs_dir, "b1b2c3d4e508", names=("vocals", "drums"))
    settings.set_stem_storage("mp3", 128)
    threading.Timer(0.5, lambda: setattr(job, "cancel_requested", True)).start()
    started = time.monotonic()
    with pytest.raises(JobCancelled):
        stem_storage.store_stems(job, jobs_dir / job.id)
    assert time.monotonic() - started < 10
    stems = jobs_dir / job.id / "stems"
    assert sorted(p.name for p in stems.iterdir()) == ["drums.wav", "vocals.wav"]


def test_an_unexpected_error_still_keeps_the_wavs(jobs_dir, monkeypatch):
    def boom(job_, wav, mp3):
        raise MemoryError("libsndfile ran out")

    monkeypatch.setattr(stem_storage, "verify_mp3", boom)
    _stub(monkeypatch, "import sys; open(sys.argv[1], 'wb').write(b'x')")
    job = _job(jobs_dir, "b1b2c3d4e509", names=("vocals",))
    _store(job, jobs_dir)
    _assert_kept_as_wav(job, jobs_dir, ("vocals",))


def test_the_runner_never_fails_a_song_over_storage(jobs_dir, monkeypatch):
    from app.pipeline import runner

    def explode(job_, job_dir):
        raise RuntimeError("storage bug")

    monkeypatch.setattr(runner, "store_stems", explode)
    job = _job(jobs_dir, "b1b2c3d4e50a", names=("vocals",))
    runner._store_stems(job, jobs_dir / job.id)
    assert job.stem_format == "wav"
    assert "store" in job.stage_timings


# ── Readers on an MP3 song ──


@pytest.fixture
def mp3_job(jobs_dir):
    skip_without_ffmpeg()
    job = _job(jobs_dir, "c1b2c3d4e5f0", names=("vocals", "drums", "bass"))
    _store(job, jobs_dir, "mp3", 128)
    assert job.stem_format == "mp3"
    return job


@pytest.fixture
def wav_job(jobs_dir):
    job = _job(jobs_dir, "c1b2c3d4e5f1", names=("vocals", "drums", "bass"))
    _store(job, jobs_dir, "wav")
    return job


async def test_stem_url_serves_the_mp3_with_its_own_type(client, mp3_job, jobs_dir):
    stored = (jobs_dir / mp3_job.id / "stems" / "vocals.mp3").read_bytes()
    async with client as c:
        for url in (f"/api/jobs/{mp3_job.id}/stems/vocals.wav", mp3_job.stems[0]["url"]):
            r = await c.get(url)
            assert r.status_code == 200
            assert r.headers["content-type"] == "audio/mpeg"
            assert ".mp3" in r.headers["content-disposition"]
            # The stored file itself, never encoded a second time.
            assert r.content == stored


async def test_stem_url_of_a_wav_song_is_unchanged(client, wav_job, jobs_dir):
    async with client as c:
        r = await c.get(f"/api/jobs/{wav_job.id}/stems/vocals.wav")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"
        assert r.content == (jobs_dir / wav_job.id / "stems" / "vocals.wav").read_bytes()


async def test_a_wav_song_with_a_cached_download_mp3_still_plays_its_wav(client, wav_job, jobs_dir):
    skip_without_ffmpeg()
    async with client as c:
        r = await c.get(f"/api/jobs/{wav_job.id}/stems/vocals.mp3")
        assert r.status_code == 200
        assert (jobs_dir / wav_job.id / "stems" / "vocals.mp3").is_file()
        r = await c.get(f"/api/jobs/{wav_job.id}/stems/vocals.wav")
        assert r.headers["content-type"] == "audio/wav"


async def test_region_of_an_mp3_stem_is_a_wav_of_the_right_samples(client, mp3_job, jobs_dir):
    ref = _signal(seed=1)
    async with client as c:
        r = await c.get(f"/api/jobs/{mp3_job.id}/stems/vocals.wav?start=2&end=4")
    assert r.status_code == 200
    data, rate = sf.read(io.BytesIO(r.content))
    assert rate == SR and len(data) == 2 * SR
    assert _lag(ref[2 * SR : 4 * SR, 0], data[:, 0]) == 0


async def test_mixdown_from_mp3_stems_lines_up_with_one_from_wavs(client, mp3_job, wav_job):
    """The export path: same lanes, same gains; the MP3 song's mix has the
    WAV song's length and its peak at lag 0."""
    q = "stems=vocals,drums,bass&gains=1,0.5,0.8"
    async with client as c:
        a = await c.get(f"/api/jobs/{wav_job.id}/mixdown.wav?{q}")
        b = await c.get(f"/api/jobs/{mp3_job.id}/mixdown.wav?{q}")
    assert a.status_code == b.status_code == 200
    x, _ = sf.read(io.BytesIO(a.content))
    y, _ = sf.read(io.BytesIO(b.content))
    assert len(x) == len(y)
    assert _lag(x[:, 0], y[:, 0]) == 0


async def test_zip_of_an_mp3_song(client, mp3_job, jobs_dir):
    async with client as c:
        r = await c.get(f"/api/jobs/{mp3_job.id}/stems/all.zip?format=mp3")
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = sorted(zf.namelist())
            assert names == ["Storage_bass.mp3", "Storage_drums.mp3", "Storage_vocals.mp3"]
            stored = (jobs_dir / mp3_job.id / "stems" / "vocals.mp3").read_bytes()
            assert zf.read("Storage_vocals.mp3") == stored
        r = await c.get(f"/api/jobs/{mp3_job.id}/stems/all.zip?format=wav")
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            data, rate = sf.read(io.BytesIO(zf.read("Storage_vocals.wav")))
            assert rate == SR and len(data) == int(SR * SECONDS)


def test_backing_and_rest_from_mp3_stems(mp3_job, jobs_dir):
    from app.pipeline.backing import ensure_backing, ensure_rest

    job_dir = jobs_dir / mp3_job.id
    out, built = ensure_backing(mp3_job.id, job_dir, "vocals")
    assert built and sf.info(str(out)).frames == int(SR * SECONDS)
    out, _ = ensure_rest(mp3_job.id, job_dir, ("bass", "drums"))
    assert sf.info(str(out)).frames == int(SR * SECONDS)


def test_take_length_comes_from_an_mp3_stem(mp3_job):
    from app.api.takes import _song_frames

    assert _song_frames(mp3_job.id) == int(SR * SECONDS)


async def test_vocal_envelope_from_an_mp3_stem(client, mp3_job):
    async with client as c:
        r = await c.get(f"/api/jobs/{mp3_job.id}/vocal-envelope")
    assert r.status_code == 200
    assert len(r.json()["db"]) > 0


def test_recovery_and_sweep_see_an_mp3_song(mp3_job, jobs_dir):
    from app.core.registry import _recover_done_job
    from app.pipeline.collect import _has_stems_on_disk

    job_dir = jobs_dir / mp3_job.id
    (job_dir / "metadata.json").write_text(
        json.dumps({"title": "Storage", "stem_format": "mp3", "stem_mp3_kbps": 128}),
        encoding="utf-8",
    )
    assert _has_stems_on_disk(job_dir)
    recovered = _recover_done_job(job_dir)
    assert recovered is not None
    assert {s["name"] for s in recovered.stems} == {"vocals", "drums", "bass"}
    assert all(s["url"].endswith(".mp3") for s in recovered.stems)
    assert (recovered.stem_format, recovered.stem_mp3_kbps) == ("mp3", 128)


def test_missing_peaks_are_rebuilt_from_mp3_stems(mp3_job, jobs_dir):
    from app.pipeline.collect import fill_missing_peaks

    stems = jobs_dir / mp3_job.id / "stems"
    assert fill_missing_peaks(stems)
    assert set(json.loads((stems / "peaks.json").read_text())) == {"vocals", "drums", "bass"}


def test_split_stems_are_stored_like_the_song(mp3_job, jobs_dir):
    stems = jobs_dir / mp3_job.id / "stems"
    for i, name in enumerate(("lead_vocals", "backing_vocals")):
        sf.write(stems / f"{name}.wav", _signal(seed=10 + i), SR, subtype="PCM_16")
    stem_storage.store_split_stems(mp3_job, stems, ["lead_vocals", "backing_vocals"])
    assert (stems / "lead_vocals.mp3").is_file() and not (stems / "lead_vocals.wav").exists()
    assert (stems / "backing_vocals.mp3").is_file()


def test_vocal_split_hands_the_model_a_decoded_wav(mp3_job, jobs_dir, monkeypatch):
    from app.pipeline import vocal_split

    seen = {}

    def fake(job, vocals_path, stems_dir):
        seen["path"] = vocals_path
        seen["frames"] = sf.info(str(vocals_path)).frames
        return ["lead_vocals", "backing_vocals"]

    monkeypatch.setattr(vocal_split, "_split", fake)
    vocal_split.split_vocals(mp3_job, jobs_dir / mp3_job.id / "stems")
    assert seen["path"].suffix == ".wav"
    assert seen["frames"] == int(SR * SECONDS)
    assert not seen["path"].exists()  # the temporary decode is gone
