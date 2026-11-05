"""The limiter at the end of every export: no clipped mix, in any format.

A lossless export (WAV, FLAC) is held at -1 dBFS sample peak and is
bit-identical below it. A lossy one (MP3, OGG, the AAC in an MP4) is held at
-1.5 dBTP true peak, because the encoder adds peak of its own; what is
checked is the true peak after decoding. Every loud test here fails without the limiter, and the lossy
ones also fail with the old sample-peak limiter on lossy formats.

The tones are chosen for that: a 11025 Hz sine (a quarter of the rate)
sampled 45 degrees off its crest reads 3 dB under its true peak, so a
limiter that only sees the samples lets the true peak through.

Set STEMDECK_TEST_SONG_STEMS to a folder of real stems (vocals.wav,
drums.wav, bass.wav, guitar.wav, piano.wav, other.wav at 44.1 kHz) to run
the same checks on a real song as well; without it those tests skip.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pytest
import soundfile as sf

from app.api import stems as stems_mod
from app.core.config import (
    EBUR128_FRAMELOG,
    EXPORT_LIMIT_DBFS,
    EXPORT_LIMIT_LOSSY_DBTP,
    ffmpeg_executable,
)
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import take_render
from app.pipeline.take_render import (
    AAC_ENCODE_ARGS,
    EXPORT_LIMITER,
    EXPORT_LIMITER_LOSSY,
    AudioLane,
    build_video_render_cmd,
    export_limiter,
)
from tests.ffmpeg_probe import skip_without_ffmpeg, skip_without_vorbis, vorbis_available

JOB = "abcdef0c11b0"
SR = 44100
SIX = ("vocals", "drums", "bass", "guitar", "piano", "other")
# What a lossy export must decode to at worst. The limit is -1.5 dBTP and the
# encoders measured add 0.3 to 0.6 dB on top (OGG and AAC at -0.9 dBTP on a
# real song), so this keeps half a decibel of headroom. Without the limiter
# the tones below decode at +4.6 dBTP; with a -1 dBFS sample-peak limiter,
# about +2.
LOSSY_DECODED_MAX_DBTP = -0.5


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", tmp_path / "cache" / "click")
    from app.core import config

    return config.JOBS_DIR


@pytest.fixture
def client(jobs_dir):
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _job(jobs_dir, lanes: dict[str, np.ndarray]) -> Path:
    first = next(iter(lanes.values()))
    job = Job(id=JOB, status="done", title="Loud", duration_sec=len(first) / SR)
    _jobs[job.id] = job
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    for name, x in lanes.items():
        data = x if x.ndim == 2 else np.stack([x, x], 1)
        sf.write(stems / f"{name}.wav", data, SR, subtype="PCM_16")
        job.stems.append({"name": name, "url": f"/api/jobs/{JOB}/stems/{name}.wav"})
    return stems


def _loud_lanes(seconds: float = 2.0) -> dict[str, np.ndarray]:
    """Three lanes that sum to about +1.6 dBFS sample and +4.6 dBTP: a
    quarter-rate sine off its crest, a low sine and a little noise."""
    t = np.arange(int(SR * seconds)) / SR
    rng = np.random.default_rng(7)
    return {
        "vocals": 0.6 * np.sin(2 * np.pi * (SR / 4) * t + np.pi / 4),
        "drums": 0.4 * np.sin(2 * np.pi * 100 * t),
        "bass": 0.1 * rng.uniform(-1, 1, len(t)),
    }


def _measure(path: Path) -> dict[str, float]:
    """Integrated loudness and true peak (ebur128, 4x), and the decoded
    samples at or over 16-bit full scale."""
    run = subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-nostats",
            "-hide_banner",
            "-i",
            str(path),
            "-vn",
            "-af",
            f"ebur128=peak=true:framelog={EBUR128_FRAMELOG}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    summary = run.stderr[run.stderr.rfind("Summary:") :]
    loudness = float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1))
    true_peak = float(re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf)", summary).group(1))
    pcm = subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vn",
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        timeout=120,
        check=True,
    ).stdout
    x = np.frombuffer(pcm, dtype="<f4")
    return {
        "I": loudness,
        "tp": true_peak,
        "full_scale": int((np.abs(x) >= 32767 / 32768).sum()),
        "peak_db": float(20 * np.log10(np.abs(x).max())),
    }


async def _export(c, tmp_path, ext: str, names, gains) -> Path:
    q = f"stems={','.join(names)}&gains={','.join(str(g) for g in gains)}"
    r = await c.get(f"/api/jobs/{JOB}/mixdown.{ext}?{q}")
    assert r.status_code == 200, r.text
    out = tmp_path / f"export.{ext}"
    out.write_bytes(r.content)
    return out


# ─── The filters themselves ──────────────────────────────────────────────


def test_lossless_limiter_is_the_sample_peak_one():
    assert export_limiter(lossy=False) == EXPORT_LIMITER
    assert "aresample" not in EXPORT_LIMITER
    assert f"limit={10 ** (EXPORT_LIMIT_DBFS / 20):.6f}" in EXPORT_LIMITER
    # level=disabled: never re-normalise; latency=1: no look-ahead delay.
    assert ":level=disabled:latency=1" in EXPORT_LIMITER


def test_lossy_limiter_is_oversampled_and_lower():
    assert export_limiter(lossy=True) == EXPORT_LIMITER_LOSSY
    assert EXPORT_LIMIT_LOSSY_DBTP < EXPORT_LIMIT_DBFS
    expected = (
        "aresample=176400,"
        f"alimiter=limit={10 ** (EXPORT_LIMIT_LOSSY_DBTP / 20):.6f}:level=disabled:latency=1,"
        "aresample=44100"
    )
    assert expected == EXPORT_LIMITER_LOSSY


def test_every_mp4_writes_aac_without_intensity_stereo(tmp_path):
    assert AAC_ENCODE_ARGS[AAC_ENCODE_ARGS.index("-aac_is") + 1] == "0"
    cmd = build_video_render_cmd(
        [AudioLane(tmp_path / "drums.wav", 1.0, "")],
        tmp_path / "audio.wav",
        1.0,
        tmp_path / "camera.mp4",
        0.0,
        2.0,
        "16:9",
        "cover",
        False,
        44100,
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert f"normalize=0,{EXPORT_LIMITER_LOSSY}[aout]" in graph
    joined = " ".join(cmd)
    assert " ".join(AAC_ENCODE_ARGS) in joined


# ─── Audio exports ───────────────────────────────────────────────────────


@pytest.mark.parametrize("ext", ["wav", "flac"])
async def test_a_loud_lossless_export_is_held_at_minus_one_dbfs(jobs_dir, client, tmp_path, ext):
    skip_without_ffmpeg()
    _job(jobs_dir, _loud_lanes())
    async with client as c:
        out = await _export(c, tmp_path, ext, ["vocals", "drums", "bass"], [1, 1, 1])
    m = _measure(out)
    assert m["full_scale"] == 0
    assert -1.1 < m["peak_db"] <= EXPORT_LIMIT_DBFS + 0.01


@pytest.mark.parametrize("ext", ["mp3", "ogg"])
async def test_a_loud_lossy_export_decodes_under_the_true_peak_ceiling(
    jobs_dir, client, tmp_path, ext
):
    """The quarter-rate tone puts the true peak 3 dB over the samples: a
    sample-peak limit at -1 dBFS decodes well over -1 dBTP here."""
    skip_without_ffmpeg()
    if ext == "ogg":
        skip_without_vorbis()
    _job(jobs_dir, _loud_lanes())
    async with client as c:
        out = await _export(c, tmp_path, ext, ["vocals", "drums", "bass"], [1, 1, 1])
    m = _measure(out)
    assert m["full_scale"] == 0
    assert m["tp"] <= LOSSY_DECODED_MAX_DBTP


async def test_a_quiet_lossy_export_keeps_its_loudness(jobs_dir, client, tmp_path):
    """Under the ceiling the lossy limiter only resamples: the MP3 measures
    the same loudness as the bit-exact WAV."""
    skip_without_ffmpeg()
    lanes = {k: 0.25 * v for k, v in _loud_lanes().items()}
    _job(jobs_dir, lanes)
    async with client as c:
        wav = await _export(c, tmp_path, "wav", ["vocals", "drums", "bass"], [1, 1, 1])
        mp3 = await _export(c, tmp_path, "mp3", ["vocals", "drums", "bass"], [1, 1, 1])
    assert abs(_measure(wav)["I"] - _measure(mp3)["I"]) <= 0.1


_CLICK_GRID = {
    "version": 1,
    "beats": [0.25, 0.75, 1.25, 1.75],
    "bars": [{"beat": 0, "beats_per_bar": 4}],
    "duration": 2.0,
}


@pytest.mark.parametrize("ext", ["wav", "flac", "mp3", "ogg"])
async def test_the_loudest_click_on_loud_stems_stays_under_the_ceiling(
    jobs_dir, client, tmp_path, ext
):
    """The click at its maximum volume (4), accented, on stems that already
    sum over full scale, in the mix and alone in the stems zip: the export
    limiter still holds. The click reaches both channels at unity (#833), 3 dB
    hotter than before, so this is its worst case."""
    skip_without_ffmpeg()
    if ext == "ogg":
        skip_without_vorbis()
    stems = _job(jobs_dir, _loud_lanes())
    (stems / "beats.json").write_text(json.dumps(_CLICK_GRID), encoding="utf-8")
    click = "click=1&click_gain=4&click_accent=4"
    async with client as c:
        r = await c.get(
            f"/api/jobs/{JOB}/mixdown.{ext}?stems=vocals,drums,bass&gains=1,1,1&{click}"
        )
        assert r.status_code == 200, r.text
        mix = tmp_path / f"mix.{ext}"
        mix.write_bytes(r.content)
        r = await c.get(f"/api/jobs/{JOB}/stems/all.zip?format={ext}&{click}")
        assert r.status_code == 200, r.text
    alone = tmp_path / f"click.{ext}"
    alone.write_bytes(zipfile.ZipFile(io.BytesIO(r.content)).read(f"Loud_click.{ext}"))
    for out in (mix, alone):
        m = _measure(out)
        assert m["full_scale"] == 0, out.name
        if ext in ("wav", "flac"):
            assert m["peak_db"] <= EXPORT_LIMIT_DBFS + 0.01, (out.name, m)
        else:
            assert m["tp"] <= LOSSY_DECODED_MAX_DBTP, (out.name, m)


async def test_the_default_click_alone_never_reaches_the_limiter(jobs_dir, client, tmp_path):
    """At the default volume (0.6) the accented click alone peaks well under
    -1 dBFS, so the lossless limiter leaves it bit-exact."""
    skip_without_ffmpeg()
    stems = _job(jobs_dir, {"drums": np.zeros(2 * SR)})
    (stems / "beats.json").write_text(json.dumps(_CLICK_GRID), encoding="utf-8")
    async with client as c:
        r = await c.get(
            f"/api/jobs/{JOB}/mixdown.wav?stems=drums&gains=0&click=1&click_gain=0.6&click_accent=4"
        )
    y, _ = sf.read(io.BytesIO(r.content))
    assert 20 * np.log10(np.abs(y).max()) < EXPORT_LIMIT_DBFS - 3


async def test_the_lossy_limiter_keeps_length_and_timing(jobs_dir, client, tmp_path):
    """latency=1 and the resample round trip must not shift or pad the
    mix: a click at a known sample stays on it, the length is exact."""
    skip_without_vorbis()
    x = np.zeros(SR)
    x[12345] = 0.9
    _job(jobs_dir, {"vocals": x, "drums": np.full(SR, 0.3)})
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/mixdown.ogg?stems=vocals,drums&gains=1,1")
    assert r.status_code == 200
    y, rate = sf.read(io.BytesIO(r.content), dtype="float64")
    assert rate == SR and abs(len(y) - SR) <= 1
    assert abs(int(np.argmax(np.abs(y[:, 0] - np.median(y[:, 0])))) - 12345) <= 2


# ─── MP4 exports ─────────────────────────────────────────────────────────


def _tiny_video(path: Path, seconds: float) -> None:
    subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=64x64:d={seconds}:r=10",
            "-c:v",
            "mpeg4",
            "-an",
            str(path),
        ],
        check=True,
        timeout=60,
    )


async def test_a_loud_video_export_decodes_under_the_true_peak_ceiling(jobs_dir, client, tmp_path):
    skip_without_ffmpeg()
    _job(jobs_dir, _loud_lanes())
    _tiny_video(jobs_dir / JOB / "video.mp4", 2.0)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/video.mp4?stems=vocals,drums,bass&gains=1,1,1")
    assert r.status_code == 200
    out = tmp_path / "out.mp4"
    out.write_bytes(r.content)
    m = _measure(out)
    assert m["full_scale"] == 0
    assert m["tp"] <= LOSSY_DECODED_MAX_DBTP


# ─── A real song ─────────────────────────────────────────────────────────


def _real_stems() -> Path:
    folder = os.environ.get("STEMDECK_TEST_SONG_STEMS", "").strip()
    if not folder or not all((Path(folder) / f"{n}.wav").is_file() for n in SIX):
        pytest.skip("STEMDECK_TEST_SONG_STEMS is not set to a folder of real stems")
    return Path(folder)


def _real_job(jobs_dir) -> None:
    src = _real_stems()
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    job = Job(id=JOB, status="done", title="Real", duration_sec=sf.info(src / "drums.wav").duration)
    _jobs[job.id] = job
    for name in SIX:
        shutil.copyfile(src / f"{name}.wav", stems / f"{name}.wav")
        job.stems.append({"name": name, "url": f"/api/jobs/{JOB}/stems/{name}.wav"})


async def test_a_real_song_boosted_never_clips_in_any_format(jobs_dir, client, tmp_path):
    """Vocals, drums and bass at +6 dB: tens of thousands of full-scale
    samples without the limiter on the songs measured."""
    skip_without_ffmpeg()
    _real_job(jobs_dir)
    gains = [1.995, 1.995, 1.995, 1, 1, 1]
    async with client as c:
        # OGG only where this ffmpeg has libvorbis (#824).
        for ext in ("wav", "flac", "mp3", *(("ogg",) if vorbis_available() else ())):
            m = _measure(await _export(c, tmp_path, ext, SIX, gains))
            assert m["full_scale"] == 0, ext
            if ext in ("wav", "flac"):
                assert m["peak_db"] <= EXPORT_LIMIT_DBFS + 0.01, ext
            else:
                assert m["tp"] <= LOSSY_DECODED_MAX_DBTP, ext


async def test_a_real_song_under_the_ceiling_is_bit_identical(jobs_dir, client, tmp_path):
    skip_without_ffmpeg()
    _real_job(jobs_dir)
    async with client as c:
        out = await _export(c, tmp_path, "wav", SIX, [0.4] * 6)
    y, _ = sf.read(out, dtype="int16")
    stems = jobs_dir / JOB / "stems"
    total = sum(sf.read(stems / f"{n}.wav", dtype="float64")[0] for n in SIX) * 0.4
    if np.abs(total).max() >= 10 ** (EXPORT_LIMIT_DBFS / 20):
        pytest.skip("this song reaches the ceiling even at 0.4")
    assert len(y) == len(total)
    assert np.abs(y.astype(np.float64) / 32768 - total).max() <= 1.5 / 32768


def test_take_render_uses_the_shared_lossy_limiter():
    """The Make video render and video.mp4 share one limiter definition."""
    assert stems_mod.EXPORT_LIMITER_LOSSY is take_render.EXPORT_LIMITER_LOSSY
