"""The camera-to-take offset, measured from the camera file's own sound.

Synthetic cases, built with numpy (and ffmpeg for real containers): a take
whose camera microphone hears the same playing as the take's input, directly
or through a room with reverb, a cheap microphone's band and noise, at a known
shift; and the cases that must fall back to the estimate: silence, unrelated
sound, no audio stream. The acceptance thresholds in app/pipeline/av_sync.py
were set from these rooms over 60 seeds each (see the comment there).
"""

from __future__ import annotations

import subprocess

import numpy as np
import pytest
import soundfile as sf

from app.core.config import ffmpeg_executable
from app.pipeline import av_sync
from app.pipeline.av_sync import SYNC_RATE, measure_offset, measure_take_offset
from tests.ffmpeg_probe import skip_without_ffmpeg

# ─── Synthetic playing ───────────────────────────────────────────────────


def music(sec: float, seed: int, rate: int = SYNC_RATE) -> np.ndarray:
    """Plucked notes with harmonics at random pitches and onsets, about four
    a second: enough like playing that its correlation is not a clean pulse."""
    g = np.random.default_rng(seed)
    n = int(sec * rate)
    x = np.zeros(n)
    t = 0.0
    while t < sec - 1.0:
        f0 = 110.0 * 2 ** (g.integers(0, 36) / 12)
        length = int(rate * g.uniform(0.3, 1.2))
        tt = np.arange(length) / rate
        note = np.zeros(length)
        for h in range(6):
            if f0 * (h + 1) < rate / 2:
                note += 0.6**h * np.sin(2 * np.pi * f0 * (h + 1) * tt + g.uniform(0, 6))
        note *= np.exp(-tt * g.uniform(3, 8)) * g.uniform(0.2, 0.8)
        i = int(t * rate)
        x[i : i + length] += note[: n - i]
        t += g.exponential(0.25)
    return x


def _band(x: np.ndarray, rate: int, lo: float, hi: float) -> np.ndarray:
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1.0 / rate)
    # A soft webcam-like band: first-order roll-offs either side.
    spec *= 1.0 / np.sqrt(1 + (f / hi) ** 2) * (f / lo) / np.sqrt(1 + (f / lo) ** 2)
    return np.fft.irfft(spec, len(x))


def room(
    x: np.ndarray,
    seed: int,
    rt60: float = 0.6,
    snr_db: float = 0.0,
    tail: float = 0.15,
    rate: int = SYNC_RATE,
) -> np.ndarray:
    """`x` as a camera across the room hears it: the direct sound, three
    early reflections, a diffuse tail (`tail` 0.15 puts it 9 dB over the
    direct sound), a 150 Hz to 2 kHz microphone and white noise."""
    g = np.random.default_rng(seed)
    length = int(rate * rt60 * 1.2)
    tt = np.arange(length) / rate
    ir = g.standard_normal(length) * np.exp(-6.9 * tt / rt60) * tail
    ir[0] = 1.0
    for d, a in ((0.004, 0.6), (0.011, 0.45), (0.017, 0.35)):
        ir[int(d * rate)] += a
    n = 1
    while n < len(x) + length:
        n *= 2
    y = np.fft.irfft(np.fft.rfft(x, n) * np.fft.rfft(ir, n), n)[: len(x)]
    y = _band(y, rate, 150.0, 2000.0)
    p = float(np.mean(y**2))
    return 0.3 * (y + g.standard_normal(len(y)) * np.sqrt(p / 10 ** (snr_db / 10)))


def shifted(src: np.ndarray, shift_ms: float, sec: float, rate: int = SYNC_RATE):
    """(raw, cam): raw is `src` from its sample 0; cam's sample n is raw's
    sample n + shift (the camera file starts `shift_ms` after raw frame 0;
    negative: before it)."""
    s = int(round(shift_ms * rate / 1000.0))
    n = int(sec * rate)
    raw = src[:n].copy()
    cam = src[s : s + n].copy() if s >= 0 else np.concatenate([np.zeros(-s), src[: n + s]])
    return raw, cam


# ─── The pure measurement ────────────────────────────────────────────────


# Measured on the maintainer's StreamCam: the camera file started 70 ms
# before raw frame 0, and the browser estimated +456 ms. And a slow camera
# whose file starts well after the take.
@pytest.mark.parametrize("shift_ms", [-70.0, 0.0, 350.0, 1200.0])
@pytest.mark.parametrize(
    ("label", "rt60", "snr_db", "tail"),
    [
        ("direct", None, None, None),
        ("small room", 0.4, 10.0, 0.05),
        ("live room", 0.6, 0.0, 0.15),
        ("reverberant and noisy", 0.8, -5.0, 0.15),
    ],
)
def test_the_offset_is_found_within_a_millisecond(shift_ms, label, rt60, snr_db, tail):
    raw, cam = shifted(music(36, 11), shift_ms, 32)
    if rt60 is not None:
        cam = room(cam, 3, rt60, snr_db, tail)
    # The estimate is 400 ms off, as on real hardware.
    r = measure_offset(cam, raw, SYNC_RATE, estimate_ms=shift_ms + 400.0)
    assert r.ok, (label, r)
    assert r.reason == "ok"
    assert abs(r.offset_ms - shift_ms) < 1.0, (label, r.offset_ms)
    assert 0.5 <= r.confidence <= 1.0
    assert r.metrics["pmr"] >= av_sync.MIN_PMR


def test_an_inverted_microphone_still_matches():
    raw, cam = shifted(music(36, 12), -70.0, 32)
    r = measure_offset(-room(cam, 4, 0.4, 10.0, 0.05), raw, SYNC_RATE, estimate_ms=300.0)
    assert r.ok and abs(r.offset_ms + 70.0) < 1.0


def test_a_take_that_starts_quietly_is_matched_in_a_later_window():
    """Silence on the input for the first 40 s: the second window has the
    playing, and the offset is still the file's t=0."""
    src = music(80, 13)
    src[: 40 * SYNC_RATE] = 0.0
    raw, cam = shifted(src, 120.0, 75)
    cam = room(cam, 5, 0.6, 0.0, 0.15)
    r = measure_offset(cam, raw, SYNC_RATE, estimate_ms=0.0)
    assert r.ok, r
    assert abs(r.offset_ms - 120.0) < 1.0
    assert r.metrics["windowStartSec"] >= 30.0


@pytest.mark.parametrize("seed", range(4))
def test_unrelated_sound_is_refused(seed):
    raw = music(32, 100 + seed)
    other = room(music(32, 900 + seed), seed, 0.6, 5.0)
    r = measure_offset(other, raw, SYNC_RATE, estimate_ms=450.0)
    assert not r.ok
    assert r.reason == "low-confidence"
    assert r.offset_ms is None
    assert r.confidence is not None and r.confidence < 0.5


def test_noise_against_playing_is_refused():
    raw = music(32, 21)
    noise = 0.1 * np.random.default_rng(1).standard_normal(len(raw))
    for cam, take in ((noise, raw), (raw, noise)):
        r = measure_offset(cam, take, SYNC_RATE, estimate_ms=450.0)
        assert not r.ok and r.reason == "low-confidence"


def test_a_weak_match_falls_back():
    """The camera hears the playing far under the room noise (-25 dB SNR):
    whatever peak is found is not trusted."""
    raw, cam = shifted(music(36, 22), 100.0, 32)
    r = measure_offset(room(cam, 6, 1.0, -25.0, 0.15), raw, SYNC_RATE, estimate_ms=0.0)
    assert not r.ok
    assert r.reason == "low-confidence"


@pytest.mark.parametrize("side", ["camera", "take"])
def test_silence_falls_back(side):
    sound = music(32, 23)
    quiet = np.zeros_like(sound)
    cam, raw = (quiet, sound) if side == "camera" else (sound, quiet)
    r = measure_offset(cam, raw, SYNC_RATE, estimate_ms=0.0)
    assert not r.ok
    assert r.reason == "silent"
    assert r.confidence is None


def test_too_short_to_compare_falls_back():
    raw, cam = shifted(music(10, 24), 0.0, 3)
    r = measure_offset(cam, raw, SYNC_RATE)
    assert not r.ok and r.reason == "silent"


def test_a_peak_at_the_edge_of_the_search_is_not_trusted():
    """The true offset is 2.5 s from the estimate, outside the +-2 s search:
    never a confident wrong answer."""
    raw, cam = shifted(music(40, 25), 2500.0, 32)
    r = measure_offset(cam, raw, SYNC_RATE, estimate_ms=0.0)
    assert not r.ok


def test_clear_windows_that_disagree_fall_back():
    """The first 30 s match at +100 ms, the next 30 s at +300 ms: two clear
    answers, so neither is trusted."""
    src = music(70, 26)
    raw = src[: 66 * SYNC_RATE]
    a = int(0.1 * SYNC_RATE)
    b = int(0.3 * SYNC_RATE)
    win = int(av_sync.WINDOW_SEC * SYNC_RATE)
    cam = np.concatenate([src[a : a + win], src[win + b : 2 * win + b]])
    r = measure_offset(cam, raw, SYNC_RATE, estimate_ms=200.0)
    assert not r.ok
    assert r.reason == "low-confidence"
    assert r.offset_ms is None
    assert r.confidence < 0.5
    assert r.metrics["disagreeMs"] == pytest.approx(200.0, abs=1.0)


def test_the_earliest_clear_window_gives_the_offset_despite_drift():
    """The camera microphone runs 100 ppm fast against the take: by 150 s
    the match has moved 15 ms. The offset is that of the file's t=0, from
    the first window, not the clearest one later on."""
    src = music(190, 27)
    shift = -70.0
    n = 180 * SYNC_RATE
    # cam sample k is raw at k * (1 + 100e-6) + shift.
    pos = np.arange(n) * (1 + 100e-6) + shift * SYNC_RATE / 1000.0
    cam = np.interp(pos, np.arange(len(src)), src, left=0.0)
    r = measure_offset(cam, src[: 185 * SYNC_RATE], SYNC_RATE, estimate_ms=300.0)
    assert r.ok, r
    assert r.metrics["windowStartSec"] == 0.0
    # Within the first window the drift smears the peak over 3 ms, and the
    # direct-path pick takes its late edge (measured: +4.2 ms). A window at
    # 150 s would be 15 ms off or more.
    assert abs(r.offset_ms - shift) < 6.0, r.offset_ms


# ─── Real containers (ffmpeg) ────────────────────────────────────────────

RATE = 48000


def _camera_file(path, audio: np.ndarray, container: str, audio_delay: float = 0.0) -> None:
    """A camera file as MediaRecorder writes one: a 30 fps picture with its
    microphone, H.264 + AAC in MP4 or VP8 + Opus in WebM. `audio_delay`
    starts the sound that much after the picture in the file."""
    wav = path.with_suffix(".src.wav")
    sf.write(wav, audio.astype(np.float32), RATE, subtype="FLOAT")
    sec = len(audio) / RATE
    vcodec = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    acodec = ["-c:a", "aac", "-b:a", "128k"]
    if container == "webm":
        vcodec = ["-c:v", "libvpx", "-b:v", "500k"]
        acodec = ["-c:a", "libopus", "-b:a", "96k"]
    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=gray:s=160x120:r=30:d={sec:.3f}",
    ]
    if audio_delay:
        cmd += ["-itsoffset", f"{audio_delay:.3f}"]
    cmd += ["-i", str(wav), "-map", "0:v", "-map", "1:a", *vcodec, *acodec, str(path)]
    subprocess.run(cmd, check=True, timeout=120)
    wav.unlink()


def _raw_wav(path, audio: np.ndarray) -> None:
    sf.write(path, audio.astype(np.float32), RATE, subtype="FLOAT")


def _upsample(x: np.ndarray) -> np.ndarray:
    """SYNC_RATE to RATE by FFT zero-padding (exact for the band used)."""
    n = len(x)
    m = n * RATE // SYNC_RATE
    spec = np.fft.rfft(x)
    out = np.zeros(m // 2 + 1, dtype=complex)
    out[: len(spec)] = spec
    return np.fft.irfft(out, m) * (m / n)


@pytest.mark.parametrize("container", ["mp4", "webm"])
@pytest.mark.parametrize("audio_delay", [0.0, 0.25])
@pytest.mark.parametrize("shift_ms", [-70.0, 362.0])
def test_a_real_camera_file_is_measured_within_ten_ms(tmp_path, container, audio_delay, shift_ms):
    """Through AAC or Opus, with the sound starting at the picture or 250 ms
    after it in the file: the offset is still that of the picture's t=0."""
    skip_without_ffmpeg()
    src = music(16, 31)
    raw, cam = shifted(src, shift_ms + audio_delay * 1000.0, 12)
    cam = room(cam, 7, 0.4, 10.0, 0.05)
    camera = tmp_path / f"camera.{container}"
    _camera_file(camera, 0.5 * _upsample(cam), container, audio_delay)
    raw_wav = tmp_path / "audio.raw.wav"
    _raw_wav(raw_wav, 0.5 * _upsample(raw))
    r = measure_take_offset(camera, raw_wav, estimate_ms=shift_ms + 450.0)
    assert r.ok, r
    assert abs(r.offset_ms - shift_ms) < 10.0, r.offset_ms


def test_a_camera_file_without_sound_says_so(tmp_path):
    skip_without_ffmpeg()
    camera = tmp_path / "camera.mp4"
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
            "color=c=gray:s=160x120:r=30:d=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(camera),
        ],
        check=True,
        timeout=60,
    )
    raw_wav = tmp_path / "audio.raw.wav"
    _raw_wav(raw_wav, np.zeros(RATE * 2))
    r = measure_take_offset(camera, raw_wav, estimate_ms=0.0)
    assert not r.ok and r.reason == "no-camera-audio"


def test_an_unreadable_file_is_a_failure_not_an_error(tmp_path):
    skip_without_ffmpeg()
    camera = tmp_path / "camera.mp4"
    camera.write_bytes(b"not a video")
    raw_wav = tmp_path / "audio.raw.wav"
    _raw_wav(raw_wav, np.zeros(RATE))
    r = measure_take_offset(camera, raw_wav, estimate_ms=0.0)
    assert not r.ok and r.reason == "measure-failed"
