"""ffmpeg work for recorded takes: the playable audio and the video render.

Everything here is blocking and runs in a thread (asyncio.to_thread) from
app/api/takes.py. The command builders are pure functions so the filter
graphs can be tested without running ffmpeg.

Sign convention, shared with the capture layer in the browser: a positive
latency or offset means the material arrived late, so it is subtracted.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import soundfile as sf

from app.core.config import (
    CANVAS_SHORT_SIDES,
    EXPORT_LIMIT_DBFS,
    EXPORT_LIMIT_LOSSY_DBTP,
    TIMEOUT_FFMPEG,
    VIDEO_CPU_ENCODER,
    ffmpeg_executable,
)
from app.pipeline import video_encoder

logger = logging.getLogger("stemdeck.takes")

# The stems' own format (app/pipeline/collect.py writes pcm_s16le; Demucs
# output is 44.1 kHz stereo). A take in exactly this format needs no
# take-specific code in the chunked engine, the peaks, the mixdown or the zip.
TAKE_RATE = 44100
TAKE_CHANNELS = 2

# alimiter's limit is linear. level=disabled stops it re-normalising the
# output to 0 dB (its default does, measured -12.8 instead of -14 LUFS in the
# mastering study) and latency=1 removes its look-ahead delay, so a lane
# keeps its timing to the sample. Measured on the bundled ffmpeg 8.1: with a
# signal under the limit the output is bit-identical to the input, same
# length; two 0.7 sines summed (52152 samples at full scale without it) peak
# at exactly -1.0 dBFS with none at full scale. This one is for lossless
# exports (WAV, FLAC), where bit-identical below the ceiling matters.
EXPORT_LIMITER = f"alimiter=limit={10 ** (EXPORT_LIMIT_DBFS / 20):.6f}:level=disabled:latency=1"

# Lossy exports (MP3, OGG, AAC) limit the true peak instead: alimiter at the
# stem rate only sees the samples, and the peaks between them (up to 0.5 dB
# over, measured) plus what the encoder adds reached -0.4 dBTP after
# decoding (MP3, OGG) and +0.4 dBFS with clipped samples (AAC). At 4x the samples are the true peak (the ffmpeg docs recommend
# 2x to 4x before alimiter), and the lower ceiling leaves the encoder room.
# The round trip through 176.4 kHz is not bit-identical, which only matters
# for a lossless file: a lossy encoder rewrites every sample anyway. Cost:
# 3.7 s per 10 minutes of song, measured.
EXPORT_LIMITER_LOSSY = (
    f"aresample={4 * TAKE_RATE},"
    f"alimiter=limit={10 ** (EXPORT_LIMIT_LOSSY_DBTP / 20):.6f}:level=disabled:latency=1,"
    f"aresample={TAKE_RATE}"
)

# AAC as every MP4 export writes it. Intensity stereo is off: with it on (the
# encoder's default) the native encoder sent single blocks of a song up to
# 2.7 dB over its input, so a mix limited to -2 dBTP decoded at +0.7 dBFS.
# Without it the encoder stayed within 0.5 dB of its input on every song
# measured.
# A low-bitrate tool; at 192 kbps it saves nothing worth a click.
AAC_ENCODE_ARGS = ["-c:a", "aac", "-b:a", "192k", "-aac_is", "0"]
# The fallback for a mastered video whose AAC still decoded over its ceiling
# (stems._render_master_verified): the default "twoloop" coder sometimes
# sends a single block several dB over its input even without intensity
# stereo (measured +3.6 dBFS on a hot song limited at -1.6 dB, at a level a
# lower input did not fix: not monotonic). The "fast" coder stayed within
# 0.8 dB of its input at every level tried on four mastered songs. It is the
# fallback, not the default, because it is the encoder's simpler rate control
# (sound not compared by ear).
AAC_ENCODE_ARGS_ALT = [*AAC_ENCODE_ARGS, "-aac_coder", "fast"]


def export_limiter(lossy: bool) -> str:
    """The limiter that ends an export's audio graph, for its encoder."""
    return EXPORT_LIMITER_LOSSY if lossy else EXPORT_LIMITER


class TakeRenderError(RuntimeError):
    """ffmpeg failed. The message is for the log, never for the client.

    `code` says how, for the GPU fallback's reason: "exit" (ffmpeg failed),
    "timeout", "stall" (no progress), or "invalid-<check>" (the file failed
    video_encoder.validate_output). `stderr` is ffmpeg's last lines, for
    video_encoder.blames_encoder (#791)."""

    def __init__(self, message: str, code: str = "exit", stderr: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.stderr = stderr


def write_float_wav_header(fh, data_bytes: int, rate: int, channels: int) -> None:
    """A WAVE_FORMAT_IEEE_FLOAT (3) header for 32-bit interleaved samples.

    The stdlib wave module writes integer PCM only, and the raw capture is
    float32: kept bit-exact so a later re-render (a nudge, an import on
    another machine) starts from what the microphone delivered."""
    block_align = channels * 4
    fh.write(b"RIFF")
    fh.write(struct.pack("<I", 36 + data_bytes))
    fh.write(b"WAVE")
    fh.write(b"fmt ")
    fh.write(struct.pack("<IHHIIHH", 16, 3, channels, rate, rate * block_align, block_align, 32))
    fh.write(b"data")
    fh.write(struct.pack("<I", data_bytes))


def assemble_raw_wav(pcm: Path, dest: Path, rate: int, channels: int) -> None:
    """Wrap the uploaded float32 parts in a WAV header, atomically."""
    size = pcm.stat().st_size
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as out, pcm.open("rb") as src:
            write_float_wav_header(out, size, rate, channels)
            shutil.copyfileobj(src, out, 1024 * 1024)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def wav_frames(path: Path) -> int:
    return int(sf.info(str(path)).frames)


def sniff_container(path: Path) -> str | None:
    """ "webm" or "mp4" from the first bytes, or None.

    The browser picks the container (WebM on Chromium and Firefox, MP4 on
    Safari/WKWebView), and a declared MIME type is only a claim. The file
    name decides which demuxer the render trusts, so it comes from the bytes."""
    try:
        with path.open("rb") as fh:
            head = fh.read(12)
    except OSError:
        return None
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "mp4"
    return None


def take_audio_filter(placement_sec: float, frames: int, source_channels: int) -> str:
    """The -af chain that turns a raw take into a playable one.

    Resampled to 44.1 kHz first, then placed in output samples, so the
    placement is rounded once at the output rate (at most half a sample out)
    rather than at the capture rate and again by the resampler. A take that
    starts before song 0 (count-in, pickup notes, the latency it is
    compensated for) is trimmed; one that starts after is delayed. Padded with
    silence and cut to exactly the stems' frame count, so every lane has the
    same length.
    """
    parts: list[str] = []
    if source_channels == 1:
        # Explicit, rather than -ac 2: the mono capture goes to both channels
        # at unity, with no up-mix coefficients in between.
        parts.append("pan=stereo|c0=c0|c1=c0")
    parts.append(f"aresample={TAKE_RATE}")
    offset = int(round(placement_sec * TAKE_RATE))
    if offset < 0:
        parts.append(f"atrim=start_sample={-offset}")
        parts.append("asetpts=PTS-STARTPTS")
    elif offset > 0:
        parts.append(f"adelay=delays={offset}S:all=1")
    parts.append(f"apad=whole_len={frames}")
    parts.append(f"atrim=end_sample={frames}")
    return ",".join(parts)


def _run(cmd: list[str], timeout: int, context: str) -> None:
    proc = subprocess.run(  # noqa: S603 -- list args, no shell, trusted ffmpeg
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if proc.returncode != 0:
        tail = proc.stderr[-2000:].decode("utf-8", "replace")
        raise TakeRenderError(f"ffmpeg failed [{context}]: {tail}")


def render_take_audio(
    raw: Path,
    dest: Path,
    placement_sec: float,
    frames: int,
    source_rate: int,
    source_channels: int,
) -> None:
    """Render audio.wav from audio.raw.wav. Blocking; written atomically.

    `placement_sec` is the song time of raw frame 0, everything already
    folded in (round trip, nudge). `frames` is the stems' frame count."""
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp.wav")
    raw_end = placement_sec + wav_frames(raw) / source_rate
    try:
        if raw_end <= 0 or placement_sec * TAKE_RATE >= frames:
            # Nothing of the take lands inside the song: an all-silent lane
            # of the right length rather than an ffmpeg graph with no frames.
            cmd = [
                ffmpeg_executable(),
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r={TAKE_RATE}:cl=stereo",
                "-af",
                f"atrim=end_sample={frames}",
            ]
        else:
            cmd = [
                ffmpeg_executable(),
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                str(raw),
                "-af",
                take_audio_filter(placement_sec, frames, source_channels),
            ]
        cmd += [
            "-ac",
            str(TAKE_CHANNELS),
            "-ar",
            str(TAKE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "-y",
            str(tmp),
        ]
        _run(cmd, TIMEOUT_FFMPEG, f"take audio {raw.parent.name}")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Video render


_encoders_cached: str | None = None
_encoders_lock = threading.Lock()


def _encoders() -> str:
    """`ffmpeg -encoders`, probed once per process when it succeeds (the
    binary cannot change mid-run). A failed or timed-out probe reads as an
    empty list for this call only, so the render says the encoder is missing
    rather than starting and failing later, and the next call probes again
    (a slow first start must not disable video render for the session)."""
    global _encoders_cached
    with _encoders_lock:
        if _encoders_cached is None:
            try:
                probe = subprocess.run(  # noqa: S603 -- fixed args, trusted ffmpeg
                    [ffmpeg_executable(), "-nostdin", "-hide_banner", "-encoders"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                logger.debug(
                    "encoder probe failed; treating video render as unavailable", exc_info=True
                )
                return ""
            if probe.returncode != 0 or not probe.stdout.strip():
                logger.debug("encoder probe exit %s; not cached", probe.returncode)
                return ""
            _encoders_cached = probe.stdout
        return _encoders_cached


def libx264_available() -> bool:
    """libx264 is GPL and left out of LGPL builds (some distro and Homebrew
    variants), so it is probed like rubberband rather than assumed."""
    return bool(re.search(r"^\s*V\S*\s+libx264\b", _encoders(), re.MULTILINE))


def aac_available() -> bool:
    """ffmpeg's native AAC encoder; present in every build seen, probed anyway."""
    return bool(re.search(r"^\s*A\S*\s+aac\b", _encoders(), re.MULTILINE))


class AudioLane(NamedTuple):
    path: Path
    gain: float
    pitch_filter: str  # "" or "rubberband=...," as stems._pitch_filter builds it
    fx_filter: str = ""  # "" or "biquad=...," (the lane's EQ, stems.build_render_fx)


CANVAS_SIZES = {"16:9": (1920, 1080), "9:16": (1080, 1920)}
VIDEO_FPS = 30
# Streamer Mode's outputs: the canvas's short side, and the frame rates.
# The short side per size: app/core/config.py (one place for the server's numbers).
VIDEO_FPS_CHOICES = (24, 30, 60)


def canvas_size(canvas: str, size: str = "1080") -> tuple[int, int]:
    """(width, height) of a 16:9, 9:16 or 1:1 canvas whose short side is
    `size` (720 or 1080). 16:9 and 9:16 at 1080 are CANVAS_SIZES."""
    short = CANVAS_SHORT_SIDES[size]
    if canvas == "1:1":
        return short, short
    long = short * 16 // 9
    return (long, short) if canvas == "16:9" else (short, long)


def camera_filter(
    start_sec: float, width: int, height: int, fit: str, mirror: bool, label_in: str
) -> str:
    """Scale, crop or letterbox one camera into the canvas and place it in
    render time. `start_sec` is the render time of the camera's first frame:
    when positive, the frames are shifted later and the canvas shows black
    until they arrive; when negative, the frames before render time 0 are cut.

    The frames are shifted first and cut after, so every kept frame keeps its
    own timestamp. Cutting first and then restarting the timestamps at 0
    (trim, then setpts=PTS-STARTPTS) moved the whole camera earlier by the
    distance between the cut and the next frame: 0 to 33 ms at 30 fps,
    measured 25 ms on a recorded take. The overlay then shows each frame on
    the nearest frame of the 30 fps grid (measured), so a frame is never off
    by more than half a frame."""
    chain = [f"setpts=PTS-STARTPTS+({start_sec:.6f})/TB"]
    if start_sec < 0:
        chain.append("trim=start=0")
    if fit == "contain":
        chain.append(f"scale={width}:{height}:force_original_aspect_ratio=decrease")
        chain.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
    elif fit == "fill":
        # A picture already composed at the canvas's shape (Streamer Mode's
        # scene): only its size changes, nothing is cut or padded.
        chain.append(f"scale={width}:{height}")
    else:
        chain.append(f"scale={width}:{height}:force_original_aspect_ratio=increase")
        chain.append(f"crop={width}:{height}")
    if mirror:
        chain.append("hflip")
    chain.append("setsar=1")
    return f"{label_in}{','.join(chain)}[cam]"


def build_video_render_cmd(
    lanes: list[AudioLane],
    take_audio: Path,
    take_gain: float,
    camera: Path,
    camera_start_sec: float,
    duration_sec: float,
    canvas: str,
    fit: str,
    mirror: bool,
    audio_rate: int,
    start_sec: float = 0.0,
    take_fx: str = "",
    master_fx: str = "",
    master_chain: str = "",
    trim_db: float = 0.0,
    alt_aac: bool = False,
    size: str = "1080",
    fps: int = VIDEO_FPS,
    encoder: str = VIDEO_CPU_ENCODER,
    encoder_variant: int = 0,
) -> list[str]:
    """One ffmpeg command: backing mix + take audio + one camera tile -> MP4.

    The audio graph is the export's (per-lane pitch and gain, amix with
    normalize=0, the lossy export limiter), with the take as one more lane at no
    offset: audio.wav is already aligned to song 0. The output has no path;
    the caller appends -progress and the destination.

    `start_sec` is the song time where the video starts (0 for the whole
    song, the count-in for the take's own span) and `duration_sec` its length.
    Every lane is cut at the same whole sample, before any pitch shift, so the
    take stays on its sample against the backing; `camera_start_sec` is song
    time and is moved into the video's time here.

    `take_fx` is the take's EQ (its track's), `master_fx` the master EQ, both
    "" or a filter prefix ending in a comma, as the lanes' fx_filter.

    `master_chain` is the master (app/pipeline/mastering.py: compressor, gain,
    true-peak limiter), which replaces the lossy export limiter; `trim_db` and
    `alt_aac` are the retry of a master whose AAC decoded over its ceiling
    (stems._render_master_verified).

    `size` (720 or 1080: the canvas's short side) and `fps` (24, 30, 60)
    are Streamer Mode's output choices; the defaults are the camera take's
    1080p at 30 fps.

    `encoder` is libx264 (the default, its arguments exactly as they always
    were) or a GPU encoder from app/pipeline/video_encoder.py with the
    argument set `encoder_variant` its probe passed with. Only the last
    format filter and the codec arguments change; the graph stays on the CPU."""
    width, height = canvas_size(canvas, size)
    if fps not in VIDEO_FPS_CHOICES:
        raise ValueError(f"unsupported frame rate {fps}")
    gpu = encoder != VIDEO_CPU_ENCODER
    if gpu and not video_encoder.is_gpu_encoder(encoder):
        raise ValueError(f"unknown encoder {encoder}")
    start_sample = max(0, int(round(start_sec * TAKE_RATE)))
    start_sec = start_sample / TAKE_RATE
    # In seconds, not samples, so a lane at another rate is cut at the same
    # instant; at 44.1 kHz this is exactly start_sample (atrim rounds to the
    # nearest sample, and a sample is 22.7 microseconds).
    cut = f"atrim=start={start_sec:.6f},asetpts=PTS-STARTPTS," if start_sample else ""
    cmd: list[str] = [ffmpeg_executable(), "-nostdin", "-loglevel", "error"]
    if gpu:
        cmd += video_encoder.global_args(encoder)
    for lane in lanes:
        cmd += ["-i", str(lane.path)]
    take_idx = len(lanes)
    cam_idx = take_idx + 1
    cmd += ["-i", str(take_audio), "-i", str(camera)]

    filters = [
        f"[{i}:a]{cut}{lane.fx_filter}{lane.pitch_filter}volume={lane.gain:.6f}[a{i}]"
        for i, lane in enumerate(lanes)
    ]
    filters.append(f"[{take_idx}:a]{cut}{take_fx}volume={take_gain:.6f}[a{take_idx}]")
    n = take_idx + 1
    end = master_chain or EXPORT_LIMITER_LOSSY
    if trim_db > 0:
        end += f",volume={10.0 ** (-trim_db / 20.0):.8f}"
    if n > 1:
        labels = "".join(f"[a{i}]" for i in range(n))
        filters.append(f"{labels}amix=inputs={n}:normalize=0,{master_fx}{end}[aout]")
    else:
        filters.append(f"[a0]{master_fx}{end}[aout]")

    filters.append(f"color=c=black:s={width}x{height}:r={fps}:d={duration_sec:.6f}[bg]")
    filters.append(
        camera_filter(camera_start_sec - start_sec, width, height, fit, mirror, f"[{cam_idx}:v]")
    )
    # eof_action=pass: once the camera ends the black canvas carries on, so
    # the video always has its full length and never freezes on a last frame.
    out_format = video_encoder.output_filter(encoder) if gpu else "format=yuv420p"
    filters.append(f"[bg][cam]overlay=eof_action=pass:shortest=0,fps={fps},{out_format}[vout]")
    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
    ]
    if gpu:
        cmd += video_encoder.encoder_args(encoder, width, height, fps, encoder_variant)
    else:
        cmd += libx264_args(width, height)
    cmd += [
        "-r",
        str(fps),
        *(AAC_ENCODE_ARGS_ALT if alt_aac else AAC_ENCODE_ARGS),
        "-ar",
        str(audio_rate),
        "-t",
        f"{duration_sec:.6f}",
        # moov before mdat: the file plays while it downloads and every
        # upload site takes it. Needs a seekable output, so this renders to a
        # file, never a pipe (unlike get_video_mixdown's fragmented stream).
        "-movflags",
        "+faststart",
    ]
    return cmd


def libx264_args(width: int, height: int) -> list[str]:
    """The CPU encode's video arguments, unchanged since before the GPU path
    (a golden test holds them to the byte)."""
    return [
        "-c:v",
        "libx264",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
    ]


class RenderCancelled(Exception):
    pass


def sweep_render_temps(jobs_dir: Path) -> int:
    """Remove render temp files (".render.mp4.<hex>.tmp.mp4") under every
    take of every job: called at startup, before anything renders. Returns
    how many went."""
    gone = 0
    for path in jobs_dir.glob("*/takes/*/.render.mp4.*.tmp.mp4"):
        try:
            path.unlink()
            gone += 1
        except OSError:
            logger.warning("stale render file not removed: %s", path.name)
    return gone


def run_video_render(
    cmd: list[str],
    dest: Path,
    duration_sec: float,
    *,
    cancelled: Callable[[], bool],
    report: Callable[[float], None],
    on_proc: Callable[[subprocess.Popen | None], None],
    timeout: int,
    check: Callable[[Path], None] | None = None,
    stall_timeout: float | None = None,
) -> None:
    """Run the render to a temp file beside `dest`, reporting progress from
    ffmpeg's -progress stream, then rename it into place. Blocking.

    Raises RenderCancelled when `cancelled()` turns true (polled about every
    half second), TakeRenderError on any ffmpeg failure or timeout. Never
    leaves a partial file at `dest`.

    A GPU render also passes `check` (video_encoder.validate_output on the
    finished temp file, before it is trusted; its OutputInvalid becomes a
    TakeRenderError "invalid-<check>") and `stall_timeout`: a render whose
    progress has not moved for that long is killed ("stall"). The CPU render
    passes neither and runs exactly as before."""
    # A cancel that came before (during the encoder test, between a GPU
    # failure and the CPU render) starts nothing (#790).
    if cancelled():
        raise RenderCancelled()
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp.mp4")
    full = [*cmd, "-progress", "pipe:1", "-nostats", "-f", "mp4", "-y", str(tmp)]
    proc = subprocess.Popen(  # noqa: S603 -- list args, no shell, trusted ffmpeg
        full,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    on_proc(proc)
    stderr_tail: deque[str] = deque(maxlen=40)

    def drain_stderr() -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr_tail.append(line.rstrip())

    drain = threading.Thread(target=drain_stderr, daemon=True)
    drain.start()
    stop = threading.Event()
    # Monotonic time of the last forward step of out_time, for the stall check.
    moved = [time.monotonic(), -1]
    killed_for: list[str] = []

    def watchdog() -> None:
        # Cancellation and the overall timeout, checked off the reading
        # thread: the progress pipe can sit silent for a long time on a
        # slow encode, and a blocked readline must not delay either.
        deadline = time.monotonic() + timeout
        while not stop.wait(0.5):
            now = time.monotonic()
            stalled = stall_timeout is not None and now - moved[0] > stall_timeout
            if cancelled() or now > deadline or stalled:
                if not cancelled():
                    killed_for.append("stall" if stalled and now <= deadline else "timeout")
                if proc.poll() is None:
                    proc.kill()
                return

    guard = threading.Thread(target=watchdog, daemon=True)
    guard.start()
    ok = False
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            key, _, value = line.strip().partition("=")
            # out_time_us is microseconds; out_time_ms is too, despite its name.
            if key == "out_time_us" and value.lstrip("-").isdigit() and duration_sec > 0:
                if int(value) > moved[1]:
                    moved[0], moved[1] = time.monotonic(), int(value)
                report(max(0.0, min(0.99, int(value) / 1e6 / duration_sec)))
        proc.wait()
        drain.join(timeout=5)
        if cancelled():
            raise RenderCancelled()
        if proc.returncode != 0:
            raise TakeRenderError(
                f"video render exit {proc.returncode}: " + " | ".join(list(stderr_tail)[-8:]),
                killed_for[0] if killed_for else "exit",
                stderr="\n".join(stderr_tail),
            )
        if check is not None:
            try:
                check(tmp)
            except video_encoder.OutputInvalid as exc:
                if cancelled():
                    raise RenderCancelled() from None
                raise TakeRenderError(f"video check failed: {exc}", f"invalid-{exc.code}") from None
            if cancelled():
                raise RenderCancelled()
        os.replace(tmp, dest)
        ok = True
        report(1.0)
    finally:
        stop.set()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        on_proc(None)
        if not ok:
            tmp.unlink(missing_ok=True)


def is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
