"""ffmpeg work and the zip of a Performance take's DaVinci Resolve package.

The package (an OpenTimelineIO bundle, .otioz) holds the take's picture as
its own video file and every sound as its own WAV, all lined up so that
DaVinci Resolve puts each one on its own track starting at the same frame
(app/pipeline/resolve_timeline.py writes the timeline).

The sync rule, the same for every file: each one starts at the same song
time (the render span's start, cut to the same whole sample as the MP4
render's lanes) and is exactly N frames long, N the span's length in frames
at the take's frame rate. The picture is made constant frame rate with the
MP4 render's own graph (a frame lands on the nearest frame of the grid, half
a frame at most); the sound is never moved to the frame grid, only cut,
resampled to 48 kHz and padded or cut to N frames' worth of samples, which is
a whole number at 24, 30 and 60 fps. No timecode, no B-frames (so the MP4's
edit list is the identity, media time 0, with nothing for an importer to
read differently), no master: it cannot be split per track.

The command builders are pure, like take_render's; everything else is
blocking and runs in a thread from app/api/takes.py.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path

import soundfile as sf

from app.core.config import (
    RESOLVE_AUDIO_RATE,
    RESOLVE_VIDEO_CRF,
    RESOLVE_VIDEO_EST_BITS_PER_PIXEL,
    WAV_PLAIN_HEADER_ARGS,
    ffmpeg_executable,
    ffprobe_executable,
)
from app.pipeline.resolve_timeline import BUNDLE_VERSION, OTIO_FPS
from app.pipeline.take_render import (
    EXPORT_LIMITER,
    TAKE_RATE,
    AudioLane,
    RenderCancelled,
    TakeRenderError,
    camera_filter,
    canvas_size,
)

logger = logging.getLogger("stemdeck.takes")

# 24-bit: headroom for a take recorded quietly, and what an editor expects.
WAV_BYTES_PER_SAMPLE = 3
WAV_CHANNELS = 2
# How much of a member is copied into the zip between two looks at Cancel.
_ZIP_CHUNK = 4 * 1024 * 1024


def samples_per_frame(fps: int) -> int:
    """48 kHz samples in one frame: exact at every rate the package takes."""
    if fps not in OTIO_FPS or RESOLVE_AUDIO_RATE % fps:
        raise ValueError(f"unsupported frame rate {fps}")
    return RESOLVE_AUDIO_RATE // fps


def span_frames(duration_sec: float, fps: int) -> int:
    """The span's length in whole frames, at least one."""
    return max(1, int(round(duration_sec * fps)))


def span_start(start_sec: float) -> float:
    """The span's start on the stems' sample grid, as the MP4 render cuts its
    lanes (take_render.build_video_render_cmd), so the take stays on its
    sample against the backing."""
    return max(0, int(round(start_sec * TAKE_RATE))) / TAKE_RATE


def package_video_cmd(
    camera: Path,
    camera_start_sec: float,
    frames: int,
    fps: int,
    canvas: str,
    size: str,
    fit: str,
    mirror: bool,
) -> list[str]:
    """The picture alone: the MP4 render's canvas (black where the camera did
    not film, the camera placed by `camera_start_sec`, the time of its first
    frame from the span's start), exactly `frames` frames at `fps`, H.264
    without B-frames, a keyframe every second, no sound, no metadata. The
    caller appends the progress and the destination (run_video_render)."""
    samples_per_frame(fps)  # refuses any other rate
    if frames < 1:
        raise ValueError("the picture needs at least one frame")
    width, height = canvas_size(canvas, size)
    # One frame more than wanted, so rounding in the source never leaves the
    # file a frame short; -frames:v cuts it to exactly `frames`.
    source_sec = (frames + 1) / fps
    graph = ";".join(
        [
            f"color=c=black:s={width}x{height}:r={fps}:d={source_sec:.6f}[bg]",
            camera_filter(camera_start_sec, width, height, fit, mirror, "[0:v]"),
            f"[bg][cam]overlay=eof_action=pass:shortest=0,fps={fps},format=yuv420p[vout]",
        ]
    )
    return [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(camera),
        "-filter_complex",
        graph,
        "-map",
        "[vout]",
        "-an",
        "-c:v",
        "libx264",
        "-crf",
        str(RESOLVE_VIDEO_CRF),
        "-pix_fmt",
        "yuv420p",
        "-bf",
        "0",
        "-g",
        str(fps),
        "-r",
        str(fps),
        "-frames:v",
        str(frames),
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
    ]


def package_audio_cmd(lanes: list[AudioLane], start_sec: float, samples: int) -> list[str]:
    """One WAV from `lanes` (one lane, or several mixed as the MP4 render
    mixes them: amix without normalising): each lane cut at `start_sec`, its
    EQ, key and gain, then 48 kHz, the lossless export limiter (bit-identical
    under -1 dBFS, so nothing clips in 24-bit) and exactly `samples` samples,
    stereo 24-bit with a plain header. The caller appends the destination."""
    if not lanes:
        raise ValueError("an audio file needs at least one lane")
    if samples < 1:
        raise ValueError("an audio file needs at least one sample")
    # In seconds, so a lane at another rate (the click) is cut at the same
    # instant; take_render.build_video_render_cmd's rule.
    cut = f"atrim=start={start_sec:.6f},asetpts=PTS-STARTPTS," if start_sec > 0 else ""
    cmd: list[str] = [ffmpeg_executable(), "-nostdin", "-loglevel", "error"]
    for lane in lanes:
        cmd += ["-i", str(lane.path)]
    filters = [
        f"[{i}:a]{cut}{lane.fx_filter}{lane.pitch_filter}volume={lane.gain:.6f}[a{i}]"
        for i, lane in enumerate(lanes)
    ]
    end = (
        f"aresample={RESOLVE_AUDIO_RATE},{EXPORT_LIMITER},"
        f"apad=whole_len={samples},atrim=end_sample={samples}[aout]"
    )
    if len(lanes) > 1:
        labels = "".join(f"[a{i}]" for i in range(len(lanes)))
        filters.append(f"{labels}amix=inputs={len(lanes)}:normalize=0,{end}")
    else:
        filters.append(f"[a0]{end}")
    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[aout]",
        "-ac",
        str(WAV_CHANNELS),
        "-ar",
        str(RESOLVE_AUDIO_RATE),
        "-c:a",
        "pcm_s24le",
        *WAV_PLAIN_HEADER_ARGS,
    ]
    return cmd


def estimate_bytes(width: int, height: int, fps: int, seconds: float, wavs: int) -> int:
    """A high guess of the package's size: the picture at
    RESOLVE_VIDEO_EST_BITS_PER_PIXEL, every WAV exactly, a megabyte over."""
    video = width * height * fps * seconds * RESOLVE_VIDEO_EST_BITS_PER_PIXEL / 8
    audio = wavs * seconds * RESOLVE_AUDIO_RATE * WAV_BYTES_PER_SAMPLE * WAV_CHANNELS
    return int(video + audio) + 1024 * 1024


def check_video(path: Path, frames: int, timeout: float = 120.0) -> None:
    """Refuse a picture file that is not what the timeline says: one video
    stream, no sound, exactly `frames` frames, no B-frames, starting at 0.
    Raises TakeRenderError (the package is then not made)."""
    proc = subprocess.run(  # noqa: S603 -- list args, no shell, trusted ffprobe
        [
            ffprobe_executable(),
            "-v",
            "error",
            "-count_packets",
            "-show_entries",
            "stream=codec_type,nb_read_packets,has_b_frames,start_time",
            "-of",
            "json",
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise TakeRenderError(f"picture check failed: {proc.stderr.strip()[-400:]}")
    try:
        streams = json.loads(proc.stdout).get("streams") or []
    except json.JSONDecodeError:
        raise TakeRenderError("picture check: unreadable ffprobe output") from None
    kinds = [s.get("codec_type") for s in streams]
    if kinds != ["video"]:
        raise TakeRenderError(f"picture check: streams {kinds}")
    video = streams[0]
    got = int(video.get("nb_read_packets") or 0)
    start = float(video.get("start_time") or 0.0)
    if got != frames or int(video.get("has_b_frames") or 0) or abs(start) > 1e-6:
        raise TakeRenderError(
            f"picture check: {got} frames (wanted {frames}), "
            f"b-frames {video.get('has_b_frames')}, start {start}"
        )


def check_wav(path: Path, samples: int) -> None:
    """Refuse a WAV that is not exactly `samples` samples of 48 kHz stereo."""
    info = sf.info(str(path))
    if info.frames != samples or info.samplerate != RESOLVE_AUDIO_RATE or info.channels != 2:
        raise TakeRenderError(
            f"audio check: {path.name} {info.frames} samples at {info.samplerate} Hz "
            f"x{info.channels} (wanted {samples} at {RESOLVE_AUDIO_RATE} x2)"
        )


def otioz_temp(dest: Path) -> Path:
    """The temp name a package is built under, beside `dest`. The startup
    sweep (take_render.sweep_render_temps) knows this exact pattern."""
    return dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")


def write_otioz(
    dest: Path,
    content: str,
    media: list[tuple[str, Path]],
    cancelled: Callable[[], bool],
    report: Callable[[float], None] | None = None,
) -> None:
    """Write the bundle to a temp file beside `dest`, flush and fsync it,
    read it back and check every member, then rename it into place. Never
    leaves a partial file at `dest`; raises RenderCancelled when `cancelled()`
    turns true between two chunks.

    The layout is the OTIO file bundle's: version.txt and content.otio
    compressed, the media stored as they are in a flat media/ folder.
    `media` is (member name, file) pairs, the member names "media/<file>"."""
    if any(not name.startswith("media/") or "/" in name[6:] for name, _ in media):
        raise ValueError("media members live in media/")
    sizes = {name: path.stat().st_size for name, path in media}
    total = sum(sizes.values()) or 1
    tmp = otioz_temp(dest)
    done = 0
    ok = False
    try:
        with tmp.open("wb") as fh:
            with zipfile.ZipFile(fh, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                zf.writestr(
                    "version.txt", BUNDLE_VERSION + "\n", compress_type=zipfile.ZIP_DEFLATED
                )
                zf.writestr("content.otio", content, compress_type=zipfile.ZIP_DEFLATED)
                for name, path in media:
                    info = zipfile.ZipInfo.from_file(path, arcname=name)
                    info.compress_type = zipfile.ZIP_STORED
                    with path.open("rb") as src, zf.open(info, "w", force_zip64=True) as out:
                        while chunk := src.read(_ZIP_CHUNK):
                            if cancelled():
                                raise RenderCancelled()
                            out.write(chunk)
                            done += len(chunk)
                            if report is not None:
                                report(min(0.95, 0.95 * done / total))
            # On disk before it is checked and renamed: a crash after the
            # rename can never leave a truncated package under its name.
            fh.flush()
            os.fsync(fh.fileno())
        if cancelled():
            raise RenderCancelled()
        _verify_otioz(tmp, content, sizes)
        os.replace(tmp, dest)
        ok = True
        if report is not None:
            report(1.0)
    finally:
        if not ok:
            tmp.unlink(missing_ok=True)


def _verify_otioz(path: Path, content: str, sizes: dict[str, int]) -> None:
    """Read the bundle back: the members in order, each the size of its
    source, every CRC right, the timeline as written."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        want = ["version.txt", "content.otio", *sizes]
        if names != want:
            raise TakeRenderError(f"package check: members {names}")
        for name, size in sizes.items():
            if zf.getinfo(name).file_size != size:
                raise TakeRenderError(f"package check: {name} is not its file's size")
        bad = zf.testzip()
        if bad is not None:
            raise TakeRenderError(f"package check: {bad} is damaged")
        if zf.read("content.otio").decode("utf-8") != content:
            raise TakeRenderError("package check: the timeline differs")
