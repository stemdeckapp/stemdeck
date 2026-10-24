"""Which H.264 encoder makes the take render's video, and whether to trust it.

libx264 on the CPU is the default and the answer to every doubt. A GPU encoder
is used only when all of this holds:

1. The policy allows it: the `video_encoder` setting ("auto", "cpu" or one
   encoder), overridden by STEMDECK_VIDEO_ENCODER (config.VIDEO_ENCODER_OVERRIDE;
   "cpu" is the kill switch).
2. It is a candidate on this OS (CANDIDATES), it has not failed
   VIDEO_ENCODER_MAX_FAILURES renders in this process, and a real test encode
   (probe) at the render's own size and rate passed. `ffmpeg -encoders` is not
   trusted: a build lists NVENC on a machine with no NVIDIA card, and 4K can
   fail where 1080p works.
3. After the render, its file passes validate_output (codec, size, rate,
   length, audio, file size, a full decode). Anything else, the caller deletes
   the file and renders again on libx264 (app/api/takes.py).

Probe results are cached per (ffmpeg binary, encoder, geometry) in memory and in
a JSON file in the data dir, so a render does not pay for a probe each time.
A pass lasts a week, a failure a day, and a timeout or a busy device only ten
minutes; a new ffmpeg (another path, size, mtime or version) starts afresh.

Filters (scale, overlay, fps) stay on the CPU: only the final encode moves.
Nothing here takes a string from a request: encoder names come from the
whitelist in config.py, and every subprocess gets list arguments.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from app.core.config import (
    DATA_DIR,
    TIMEOUT_VIDEO_ENCODER_PROBE,
    TIMEOUT_VIDEO_VALIDATE_MIN,
    VIDEO_CPU_ENCODER,
    VIDEO_ENCODER_FAIL_TTL_SEC,
    VIDEO_ENCODER_MAX_FAILURES,
    VIDEO_ENCODER_OVERRIDE,
    VIDEO_ENCODER_PASS_TTL_SEC,
    VIDEO_ENCODER_PROBE_SEC,
    VIDEO_ENCODER_RETRY_SEC,
    VIDEO_ENCODER_TEST_BUDGET_SEC,
    VIDEO_GPU_ENCODERS,
    VIDEO_GPU_MAX_BITS_PER_PIXEL,
    VIDEO_VALIDATE_DURATION_TOL_SEC,
    VIDEO_VALIDATE_SEC_PER_SEC,
    ffmpeg_executable,
    ffprobe_executable,
)

logger = logging.getLogger("stemdeck.video_encoder")

CPU = VIDEO_CPU_ENCODER

# Tried in this order; libx264 is always last and needs no probe. h264_mf is
# never used (refused 4K on the test machine, no constant-quality mode).
CANDIDATES: dict[str, tuple[str, ...]] = {
    "windows": ("h264_nvenc", "h264_qsv", "h264_amf"),
    "linux": ("h264_nvenc", "h264_vaapi", "h264_qsv"),
    "macos": ("h264_videotoolbox",),
}

# The vendor behind each encoder, for the "Encoded with" note.
FAMILY = {
    "h264_nvenc": "nvidia",
    "h264_qsv": "intel",
    "h264_amf": "amd",
    "h264_vaapi": "vaapi",
    "h264_videotoolbox": "apple",
    CPU: "cpu",
}

# The two geometries the Settings test runs (and the matrix script's 1080p/4K).
TEST_SIZES = {"1080": (1920, 1080, 30), "2160": (3840, 2160, 30)}

_CACHE_VERSION = 1
_cache_path = DATA_DIR / "video_encoders.json"


def os_key(platform: str | None = None) -> str:
    plat = platform if platform is not None else sys.platform
    if plat.startswith("win"):
        return "windows"
    if plat == "darwin":
        return "macos"
    return "linux"


def candidates(platform: str | None = None) -> tuple[str, ...]:
    """The GPU encoders worth trying on this OS, in order."""
    return CANDIDATES[os_key(platform)]


def is_gpu_encoder(name: str) -> bool:
    return name in VIDEO_GPU_ENCODERS


# ---------------------------------------------------------------------------
# Encoder arguments


# H.264 levels: (name, max macroblocks per frame, per second, max High-profile
# bitrate in kbit/s). From the spec's Table A-1 (High = 1.25x the base rate).
_LEVELS = (
    ("3.1", 3600, 108000, 17500),
    ("3.2", 5120, 216000, 25000),
    ("4.0", 8192, 245760, 25000),
    ("4.1", 8192, 245760, 62500),
    ("4.2", 8704, 522240, 62500),
    ("5.0", 22080, 589824, 168750),
    ("5.1", 36864, 983040, 300000),
    # 4K at 60 fps needs 5.2 (1944000 macroblocks a second); the app does not
    # offer 4K60 today, but the table does not have to know that.
    ("5.2", 36864, 2073600, 300000),
)


def max_bitrate_kbps(width: int, height: int, fps: int) -> int:
    """The GPU encoders' bitrate ceiling for this geometry."""
    return int(width * height * fps * VIDEO_GPU_MAX_BITS_PER_PIXEL / 1000)


def h264_level(width: int, height: int, fps: int, kbps: int | None = None) -> str:
    """The lowest level that holds this frame size, rate and bitrate."""
    mbs = ((width + 15) // 16) * ((height + 15) // 16)
    rate = kbps if kbps is not None else max_bitrate_kbps(width, height, fps)
    for name, frame_mbs, mbps, max_kbps in _LEVELS:
        if mbs <= frame_mbs and mbs * fps <= mbps and rate <= max_kbps:
            return name
    return _LEVELS[-1][0]


def _level_max_kbps(level: str) -> int:
    return next(max_kbps for name, _f, _m, max_kbps in _LEVELS if name == level)


def variant_count(encoder: str) -> int:
    """How many argument sets an encoder has, tried in order by the probe:
    VideoToolbox's constant quality (-q:v) exists only on Apple Silicon, and
    NVENC's p1-p7 presets only in ffmpeg 4.3 and later."""
    return 2 if encoder in ("h264_videotoolbox", "h264_nvenc") else 1


def encoder_args(encoder: str, width: int, height: int, fps: int, variant: int = 0) -> list[str]:
    """The output's video codec arguments for a GPU encoder: constant-quality
    style rate control under a bitrate ceiling, High profile, 8-bit 4:2:0.
    Quality was matched to libx264 crf 20 on NVENC (measured); the others
    follow the same intent and are checked by the matrix script, not here.

    NVENC's preset is p4: on the RTX 3080, on the real take render command,
    p5 ran 1.93x realtime at 4K (as slow as libx264 veryfast) and p4 3.34x,
    at the same SSIM (0.99987 against a near-lossless render; libx264 crf 20
    0.99940). At 1080p p4 ran 10.2x, p5 7.2x, libx264 6.1x."""
    kbps = max_bitrate_kbps(width, height, fps)
    level = h264_level(width, height, fps, kbps)
    cap = ["-maxrate", f"{kbps}k", "-bufsize", f"{2 * kbps}k"]
    if encoder == "h264_nvenc":
        rc = (
            ["-preset", "p4", "-tune", "hq"]
            if variant == 0
            else ["-preset", "slow"]  # ffmpeg before 4.3: no p1-p7, no -tune
        )
        return [
            "-c:v",
            "h264_nvenc",
            *rc,
            "-rc",
            "vbr",
            "-cq",
            "19",
            "-b:v",
            "0",
            *cap,
            "-profile:v",
            "high",
            "-level",
            level,
            "-pix_fmt",
            "yuv420p",
        ]
    if encoder == "h264_qsv":
        # ICQ (intelligent constant quality); no look-ahead, which some
        # drivers refuse and which buys little at these rates.
        return [
            "-c:v",
            "h264_qsv",
            "-preset",
            "medium",
            "-global_quality",
            "21",
            "-look_ahead",
            "0",
            "-profile:v",
            "high",
            "-pix_fmt",
            "nv12",
        ]
    if encoder == "h264_amf":
        return [
            "-c:v",
            "h264_amf",
            "-quality",
            "quality",
            "-rc",
            "cqp",
            "-qp_i",
            "20",
            "-qp_p",
            "22",
            "-qp_b",
            "24",
            "-profile:v",
            "high",
            "-pix_fmt",
            "nv12",
        ]
    if encoder == "h264_vaapi":
        # The frames are already on the device (output_filter's hwupload).
        return [
            "-c:v",
            "h264_vaapi",
            "-rc_mode",
            "CQP",
            "-global_quality",
            "21",
            "-profile:v",
            "high",
        ]
    if encoder == "h264_videotoolbox":
        quality = ["-q:v", "65"] if variant == 0 else ["-b:v", f"{kbps // 3}k", *cap]
        return [
            "-c:v",
            "h264_videotoolbox",
            *quality,
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
        ]
    raise ValueError(f"not a GPU encoder: {encoder}")


def vaapi_device() -> str | None:
    """The first DRM render node, or None (WSL and most containers have none)."""
    nodes = sorted(Path("/dev/dri").glob("renderD*")) if os.name == "posix" else []
    return str(nodes[0]) if nodes else None


def global_args(encoder: str) -> list[str]:
    """Options that go before the inputs (VAAPI's device)."""
    if encoder == "h264_vaapi":
        device = vaapi_device()
        return ["-vaapi_device", device] if device else []
    return []


def output_filter(encoder: str) -> str:
    """The last filter of the video chain: the CPU picture as the encoder takes it."""
    return "format=nv12,hwupload" if encoder == "h264_vaapi" else "format=yuv420p"


def size_cap_bytes(encoder: str, width: int, height: int, fps: int, duration: float) -> int:
    """The largest file a GPU render of this length may honestly be: its
    level's bitrate plus the 192 kbps AAC, with half again for the container
    and rounding. Anything bigger is a runaway encoder."""
    level = h264_level(width, height, fps)
    kbps = _level_max_kbps(level) + 256
    return int(max(duration, 1.0) * kbps * 1000 / 8 * 1.5) + 1_000_000


# ---------------------------------------------------------------------------
# The ffmpeg binary's identity


_ident_cache: dict[tuple[str, int, int], tuple[str, str]] = {}
_ident_lock = threading.Lock()


def _resolve(exe: str) -> str | None:
    if os.path.isfile(exe):
        return os.path.abspath(exe)
    return shutil.which(exe)


def ffmpeg_identity() -> tuple[str, str]:
    """(id, version line) of the ffmpeg this process runs. The id changes when
    the binary does (path, size, mtime or version), which drops every cached
    probe. ("", "") when there is no binary to run."""
    path = _resolve(ffmpeg_executable())
    if path is None:
        return "", ""
    try:
        st = os.stat(path)
    except OSError:
        return "", ""
    key = (path, st.st_size, st.st_mtime_ns)
    with _ident_lock:
        hit = _ident_cache.get(key)
        if hit is not None:
            return hit
    try:
        out = subprocess.run(  # noqa: S603 -- fixed args, trusted ffmpeg
            [path, "-hide_banner", "-version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_VIDEO_ENCODER_PROBE,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("ffmpeg -version failed", exc_info=True)
        return "", ""
    version = (out.stdout.splitlines() or [""])[0].strip()[:200]
    if out.returncode != 0 or not version:
        return "", ""
    ident = hashlib.sha256(f"{path}|{st.st_size}|{st.st_mtime_ns}|{version}".encode()).hexdigest()
    result = (ident[:16], version)
    with _ident_lock:
        _ident_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# Probe


@dataclass
class ProbeResult:
    encoder: str
    width: int
    height: int
    fps: int
    ok: bool
    # "" when ok; else missing (not in this ffmpeg), nodevice, failed, busy,
    # timeout, error (could not start), disabled, budget (Test ran out of time),
    # cancelled (the render that asked was cancelled: never cached).
    reason: str = ""
    detail: str = ""  # ffmpeg's error line, cleaned (no addresses, no paths)
    speed: float | None = None  # x realtime of the test encode
    variant: int = 0
    checked_at: float = 0.0
    transient: bool = False

    def expires_at(self) -> float:
        if self.ok:
            ttl = VIDEO_ENCODER_PASS_TTL_SEC
        elif self.transient:
            ttl = VIDEO_ENCODER_RETRY_SEC
        else:
            ttl = VIDEO_ENCODER_FAIL_TTL_SEC
        return self.checked_at + ttl

    def view(self) -> dict:
        d = asdict(self)
        d.pop("transient", None)
        d.pop("checked_at", None)
        return d


# Signs of a device that is there but taken or short of memory right now (an
# NVENC session limit while OBS streams, another render): worth another try
# in a few minutes, never a lasting "does not work".
_TRANSIENT_RE = re.compile(
    r"out of memory|busy|temporarily unavailable|incompatible client key|"
    r"too many|no free|try again",
    re.IGNORECASE,
)
_MISSING_RE = re.compile(r"unknown encoder|encoder not found", re.IGNORECASE)
# ffmpeg's own noise around the real error.
_NOISE_RE = re.compile(
    r"could not open encoder before eof|task finished with error|terminating thread|"
    r"nothing was written|error while filtering|error opening output|error selecting|"
    r"conversion failed|^\s*$",
    re.IGNORECASE,
)
_PREFIX_RE = re.compile(r"^(\[[^\]]*\]\s*)+")
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]+")


def clean_error_line(stderr: str) -> str:
    """The most telling line of an ffmpeg error: the first that is not one of
    its generic wrap-up messages, without the "[enc @ 0x...]" prefixes.
    Printable ASCII, at most 160 characters."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    pick = next((ln for ln in lines if not _NOISE_RE.search(ln)), lines[0] if lines else "")
    pick = _ADDRESS_RE.sub("", _PREFIX_RE.sub("", pick))
    pick = "".join(c for c in pick if 32 <= ord(c) < 127).strip()
    return pick[:160]


def classify_failure(stderr: str) -> tuple[str, bool]:
    """(reason, transient) for an encode that exited non-zero."""
    if _MISSING_RE.search(stderr):
        return "missing", False
    if _TRANSIENT_RE.search(stderr):
        return "busy", True
    return "failed", False


# A render that failed over its files, not its encoder: ffmpeg prints the
# system's own words for these (a full disk, a missing or unreadable file, a
# failing drive), the same whichever encoder runs.
_FILES_RE = re.compile(
    r"no space left on device|disk quota exceeded|file too large|no such file or directory|"
    r"permission denied|input/output error|read-only file system|error opening input",
    re.IGNORECASE,
)


def blames_encoder(encoder: str, code: str, stderr: str) -> bool:
    """Whether a failed GPU render counts toward VIDEO_ENCODER_MAX_FAILURES
    (#791). A stall, the timeout and a file that fails the check
    ("invalid-<check>") count, as before.

    An ffmpeg exit counts only when the encoder itself said so: a line logged
    by it ("[h264_nvenc @ ...]", or ffmpeg's encoder task "[enc:h264_nvenc @
    ...]"), on a failure that is neither about the files nor a busy device
    (classify_failure). Anything else does not count: only NVENC on Windows
    has been seen failing, other vendors' words are unknown, and a wrong
    count turns the encoder off until a restart while a missed one costs a
    single quick GPU attempt before the CPU render."""
    if code != "exit":
        return True
    if _FILES_RE.search(stderr):
        return False
    reason, transient = classify_failure(stderr)
    if transient:
        return False
    if reason == "missing":
        return True
    own = re.compile(rf"\[(?:enc:)?{re.escape(encoder)} @", re.IGNORECASE)
    return own.search(stderr) is not None


def probe_cmd(encoder: str, width: int, height: int, fps: int, variant: int = 0) -> list[str]:
    """A real encode of a moving test picture at the render's geometry, thrown
    away (-f null), with -progress so the frames that came out are counted."""
    return [
        ffmpeg_executable(),
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        *global_args(encoder),
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=s={width}x{height}:r={fps}:d={VIDEO_ENCODER_PROBE_SEC}",
        "-vf",
        output_filter(encoder),
        *encoder_args(encoder, width, height, fps, variant),
        "-r",
        str(fps),
        "-progress",
        "pipe:1",
        "-nostats",
        "-f",
        "null",
        "-",
    ]


def _never() -> bool:
    return False


# How often a probe, or a render waiting for the probe lock, looks at its
# cancel flag (#790).
_CANCEL_POLL_SEC = 0.25


def _run_probe_once(
    encoder: str,
    width: int,
    height: int,
    fps: int,
    variant: int,
    cancelled: Callable[[], bool] = _never,
) -> ProbeResult:
    now = time.time()
    base = {"encoder": encoder, "width": width, "height": height, "fps": fps}
    base["variant"] = variant
    if encoder == "h264_vaapi" and vaapi_device() is None:
        return ProbeResult(**base, ok=False, reason="nodevice", checked_at=now)
    cmd = probe_cmd(encoder, width, height, fps, variant)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 -- list args from the whitelist, trusted ffmpeg
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return ProbeResult(
            **base,
            ok=False,
            reason="error",
            detail=type(exc).__name__,
            checked_at=now,
            transient=True,
        )
    stop = threading.Event()
    stopped: list[bool] = []

    def watchdog() -> None:
        # A cancel of the render that asked kills the test encode at once.
        while not stop.wait(_CANCEL_POLL_SEC):
            if cancelled():
                stopped.append(True)
                if proc.poll() is None:
                    proc.kill()
                return

    guard = threading.Thread(target=watchdog, daemon=True)
    guard.start()
    try:
        out, err = proc.communicate(timeout=TIMEOUT_VIDEO_ENCODER_PROBE)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return ProbeResult(**base, ok=False, reason="timeout", checked_at=now, transient=True)
    finally:
        stop.set()
    if stopped:
        return ProbeResult(**base, ok=False, reason="cancelled", checked_at=now, transient=True)
    wall = max(time.monotonic() - start, 1e-3)
    if proc.returncode != 0:
        reason, transient = classify_failure(err)
        return ProbeResult(
            **base,
            ok=False,
            reason=reason,
            detail=clean_error_line(err),
            checked_at=now,
            transient=transient,
        )
    frames = [int(v) for v in re.findall(r"^frame=(\d+)", out, re.MULTILINE)]
    want = VIDEO_ENCODER_PROBE_SEC * fps
    if not frames or frames[-1] < want * 0.9:
        return ProbeResult(
            **base,
            ok=False,
            reason="failed",
            detail=f"{frames[-1] if frames else 0} of {want} frames",
            checked_at=now,
        )
    return ProbeResult(**base, ok=True, speed=round(frames[-1] / fps / wall, 2), checked_at=now)


def run_probe(
    encoder: str, width: int, height: int, fps: int, cancelled: Callable[[], bool] = _never
) -> ProbeResult:
    """Probe every argument variant of `encoder` in order; the first that
    passes wins. The last failure is the answer when none does."""
    result = _run_probe_once(encoder, width, height, fps, 0, cancelled)
    for variant in range(1, variant_count(encoder)):
        # A missing encoder, no device, a timeout, a cancel: another variant
        # cannot help.
        if result.ok or result.reason in ("missing", "nodevice", "timeout", "error", "cancelled"):
            break
        result = _run_probe_once(encoder, width, height, fps, variant, cancelled)
    return result


# ---------------------------------------------------------------------------
# Probe cache

_cache: dict[str, ProbeResult] = {}
_cache_loaded_for: str | None = None
_cache_lock = threading.RLock()
_probe_lock = threading.Lock()


def _args_sig(encoder: str, width: int, height: int, fps: int) -> str:
    """A fingerprint of what a probe of this encoder runs, so a release that
    changes the arguments never trusts a pass of the old ones."""
    parts = [
        global_args(encoder),
        output_filter(encoder),
        [encoder_args(encoder, width, height, fps, v) for v in range(variant_count(encoder))],
    ]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:10]


def _key(ident: str, encoder: str, width: int, height: int, fps: int) -> str:
    sig = _args_sig(encoder, width, height, fps)
    return f"{ident}|{encoder}|{width}x{height}@{fps}|{sig}"


def _load_cache(ident: str) -> None:
    """Fill the in-memory cache from the JSON file once per ffmpeg binary,
    keeping only this binary's unexpired entries."""
    global _cache_loaded_for
    with _cache_lock:
        if _cache_loaded_for == ident:
            return
        _cache.clear()
        _cache_loaded_for = ident
        try:
            data = json.loads(_cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("video encoder cache unreadable; probing afresh", exc_info=True)
            return
        if not isinstance(data, dict) or data.get("v") != _CACHE_VERSION:
            return
        if data.get("ffmpeg") != ident:
            return
        now = time.time()
        for key, row in (data.get("entries") or {}).items():
            try:
                result = ProbeResult(**row)
            except TypeError:
                continue
            if not key.startswith(f"{ident}|") or result.encoder not in VIDEO_GPU_ENCODERS:
                continue
            if result.expires_at() > now:
                _cache[key] = result


def _save_cache(ident: str) -> None:
    with _cache_lock:
        data = {
            "v": _CACHE_VERSION,
            "ffmpeg": ident,
            "entries": {k: asdict(r) for k, r in _cache.items() if k.startswith(f"{ident}|")},
        }
    try:
        _cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path.with_name(f"{_cache_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, _cache_path)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError:
        # The in-memory copy still applies for this run.
        logger.warning("video encoder cache not written", exc_info=True)


def probe(
    encoder: str,
    width: int,
    height: int,
    fps: int,
    force: bool = False,
    cancelled: Callable[[], bool] = _never,
) -> ProbeResult:
    """The cached probe result for this encoder and geometry, probing when
    there is none, it expired, or `force`. Never raises.

    `cancelled` (a render's cancel flag) is looked at while waiting for the
    probe lock and during the test encode; a cancelled probe answers
    "cancelled" and is never cached."""
    if encoder not in VIDEO_GPU_ENCODERS:
        raise ValueError(f"not a GPU encoder: {encoder}")
    ident, _version = ffmpeg_identity()
    if not ident:
        return ProbeResult(
            encoder,
            width,
            height,
            fps,
            ok=False,
            reason="error",
            checked_at=time.time(),
            transient=True,
        )
    key = _key(ident, encoder, width, height, fps)

    def gave_up() -> ProbeResult:
        return ProbeResult(
            encoder,
            width,
            height,
            fps,
            ok=False,
            reason="cancelled",
            checked_at=time.time(),
            transient=True,
        )

    if not force:
        # A cached answer needs no probe lock, which another probe (Settings'
        # Test encoders) can hold for minutes (#791).
        hit = _valid_hit(ident, key)
        if hit is not None:
            return hit
    while not _probe_lock.acquire(timeout=_CANCEL_POLL_SEC):
        if cancelled():
            return gave_up()
    try:
        hit = _valid_hit(ident, key)
        if hit is not None and not force:
            return hit
        if cancelled():
            return gave_up()
        result = run_probe(encoder, width, height, fps, cancelled)
        if result.reason == "cancelled":
            return result
        logger.info(
            "video encoder probe %s %dx%d@%d: %s%s",
            encoder,
            width,
            height,
            fps,
            "ok" if result.ok else result.reason,
            f" ({result.detail})" if result.detail else "",
        )
        if hit is not None and hit.ok and not result.ok and result.transient:
            # A forced probe (Test encoders) that only met a busy card, a
            # timeout: the pass stands for renders; the test still reports
            # what it saw (#791).
            return result
        with _cache_lock:
            _cache[key] = result
        _save_cache(ident)
        return result
    finally:
        _probe_lock.release()


def _valid_hit(ident: str, key: str) -> ProbeResult | None:
    """The cached result for `key` while it is still trusted, else None."""
    _load_cache(ident)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and hit.expires_at() > time.time():
        return hit
    return None


def cached_results() -> list[ProbeResult]:
    """What the cache holds for this ffmpeg, without probing anything."""
    ident, _version = ffmpeg_identity()
    if not ident:
        return []
    # Only the cache: no probe lock, so a running Test never holds it (#791).
    _load_cache(ident)
    with _cache_lock:
        now = time.time()
        return [r for k, r in _cache.items() if k.startswith(f"{ident}|") and r.expires_at() > now]


# ---------------------------------------------------------------------------
# Failures in real renders

_failures: dict[str, int] = {}
_disabled: set[str] = set()
_fail_lock = threading.Lock()


def record_failure(encoder: str, reason: str) -> bool:
    """Count a GPU render that had to be made again on the CPU. Returns True
    when this one disabled the encoder for the rest of the process."""
    if encoder not in VIDEO_GPU_ENCODERS:
        return False
    with _fail_lock:
        _failures[encoder] = _failures.get(encoder, 0) + 1
        count = _failures[encoder]
        newly = count >= VIDEO_ENCODER_MAX_FAILURES and encoder not in _disabled
        if newly:
            _disabled.add(encoder)
    logger.warning(
        "GPU encoder %s failed a render (%s), %d of %d%s",
        encoder,
        reason,
        count,
        VIDEO_ENCODER_MAX_FAILURES,
        "; not used again until StemDeck restarts" if newly else "",
    )
    return newly


def disabled() -> list[str]:
    with _fail_lock:
        return sorted(_disabled)


def failure_count(encoder: str) -> int:
    with _fail_lock:
        return _failures.get(encoder, 0)


def reset_state() -> None:
    """Forget failures and the in-memory cache (tests)."""
    global _cache_loaded_for
    with _fail_lock:
        _failures.clear()
        _disabled.clear()
    with _cache_lock:
        _cache.clear()
        _cache_loaded_for = None
    with _ident_lock:
        _ident_cache.clear()


# ---------------------------------------------------------------------------
# Policy and choice


def policy() -> tuple[str, str]:
    """(value, source): the env override when set ("env"), else the setting."""
    if VIDEO_ENCODER_OVERRIDE:
        return VIDEO_ENCODER_OVERRIDE, "env"
    from app.core.settings import get_video_encoder

    return get_video_encoder(), "setting"


@dataclass
class Choice:
    encoder: str
    variant: int = 0
    # Why not a GPU encoder, for the log: "policy", "none" (nothing passed),
    # or the last probe's reason.
    reason: str = ""


def choose(width: int, height: int, fps: int, cancelled: Callable[[], bool] = _never) -> Choice:
    """The encoder for a render of this geometry. Blocking (may probe).
    Gives up with reason "cancelled" as soon as `cancelled()` is true."""
    value, _source = policy()
    if value == "cpu":
        return Choice(CPU, reason="policy")
    order = (value,) if value in VIDEO_GPU_ENCODERS else candidates()
    reason = "none"
    for encoder in order:
        if cancelled():
            return Choice(CPU, reason="cancelled")
        if encoder in disabled():
            reason = "disabled"
            continue
        result = probe(encoder, width, height, fps, cancelled=cancelled)
        if result.ok:
            return Choice(encoder, result.variant)
        if result.reason == "cancelled":
            return Choice(CPU, reason="cancelled")
        reason = result.reason or "failed"
    return Choice(CPU, reason=reason)


# ---------------------------------------------------------------------------
# The Settings test


def test_all(budget_sec: float = VIDEO_ENCODER_TEST_BUDGET_SEC) -> list[dict]:
    """Probe every candidate of this OS afresh at 1080p30 and 4K30, and
    libx264 for comparison, within `budget_sec`. One row per encoder:
    {"encoder", "family", "disabled", "results": {"1080": {...}, "2160": {...}}}."""
    deadline = time.monotonic() + budget_sec
    rows = []
    for encoder in (*candidates(), CPU):
        row = {
            "encoder": encoder,
            "family": FAMILY[encoder],
            "disabled": encoder in disabled(),
            "results": {},
        }
        for size, (w, h, fps) in TEST_SIZES.items():
            if time.monotonic() >= deadline:
                result = ProbeResult(encoder, w, h, fps, ok=False, reason="budget")
            elif encoder == CPU:
                result = _probe_cpu(w, h, fps)
            else:
                result = probe(encoder, w, h, fps, force=True)
            row["results"][size] = result.view()
        rows.append(row)
    return rows


def _probe_cpu(width: int, height: int, fps: int) -> ProbeResult:
    """libx264 with the render's own settings, for the speed to compare with."""
    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=s={width}x{height}:r={fps}:d={VIDEO_ENCODER_PROBE_SEC}",
        "-c:v",
        CPU,
        *(["-preset", "veryfast"] if min(width, height) >= 2160 else []),
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "null",
        "-",
    ]
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed args, trusted ffmpeg
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_VIDEO_ENCODER_PROBE,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(CPU, width, height, fps, ok=False, reason="timeout", transient=True)
    except OSError:
        return ProbeResult(CPU, width, height, fps, ok=False, reason="error", transient=True)
    wall = max(time.monotonic() - start, 1e-3)
    if proc.returncode != 0:
        reason, transient = classify_failure(proc.stderr)
        detail = clean_error_line(proc.stderr)
        return ProbeResult(
            CPU, width, height, fps, ok=False, reason=reason, detail=detail, transient=transient
        )
    return ProbeResult(
        CPU, width, height, fps, ok=True, speed=round(VIDEO_ENCODER_PROBE_SEC / wall, 2)
    )


# ---------------------------------------------------------------------------
# Validation of a GPU render


class OutputInvalid(Exception):
    """The file is not what was asked for. `code` names the check."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _fraction(value: object) -> float:
    try:
        num, _, den = str(value).partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _run_watched(
    cmd: list[str],
    timeout: float,
    cancelled: Callable[[], bool],
    on_proc: Callable[[subprocess.Popen | None], None],
) -> tuple[int, str, str]:
    """Run to the end; killed on timeout or cancel, which raise OutputInvalid
    ("timeout", "cancelled"). Returns (returncode, stdout, stderr)."""
    proc = subprocess.Popen(  # noqa: S603 -- list args, trusted ffmpeg/ffprobe
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    on_proc(proc)
    stop = threading.Event()
    why: list[str] = []

    def watchdog() -> None:
        deadline = time.monotonic() + timeout
        while not stop.wait(0.25):
            if cancelled() or time.monotonic() > deadline:
                why.append("cancelled" if cancelled() else "timeout")
                if proc.poll() is None:
                    proc.kill()
                return

    guard = threading.Thread(target=watchdog, daemon=True)
    guard.start()
    try:
        out, err = proc.communicate()
    finally:
        stop.set()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if why:
        raise OutputInvalid(why[0])
    return proc.returncode, out, err


def validate_output(
    path: Path,
    width: int,
    height: int,
    fps: int,
    duration: float,
    encoder: str,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    on_proc: Callable[[subprocess.Popen | None], None] = lambda p: None,
) -> None:
    """Raise OutputInvalid unless `path` is the H.264 + audio MP4 that was
    asked for and decodes cleanly from end to end. Blocking."""
    try:
        size = path.stat().st_size
    except OSError:
        raise OutputInvalid("missing") from None
    if size < max(4096, duration * 1000):
        raise OutputInvalid("filesize", f"{size} bytes")
    if size > size_cap_bytes(encoder, width, height, fps, duration):
        raise OutputInvalid("filesize", f"{size} bytes")
    rc, out, _err = _run_watched(
        [
            ffprobe_executable(),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,r_frame_rate,duration:format=duration",
            "-of",
            "json",
            str(path),
        ],
        TIMEOUT_VIDEO_ENCODER_PROBE,
        cancelled,
        on_proc,
    )
    if rc != 0:
        raise OutputInvalid("probe")
    try:
        info = json.loads(out or "{}")
    except ValueError:
        raise OutputInvalid("probe") from None
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None or video.get("codec_name") != "h264":
        raise OutputInvalid("codec", str(video and video.get("codec_name")))
    if (video.get("width"), video.get("height")) != (width, height):
        raise OutputInvalid("size", f"{video.get('width')}x{video.get('height')}")
    if abs(_fraction(video.get("r_frame_rate")) - fps) > 0.01:
        raise OutputInvalid("fps", str(video.get("r_frame_rate")))
    if not any(s.get("codec_type") == "audio" for s in streams):
        raise OutputInvalid("audio")
    got = _fraction(video.get("duration") or (info.get("format") or {}).get("duration"))
    if abs(got - duration) > max(VIDEO_VALIDATE_DURATION_TOL_SEC, 2.0 / fps):
        raise OutputInvalid("duration", f"{got:.3f} s of {duration:.3f} s")
    rc, _out, err = _run_watched(
        [ffmpeg_executable(), "-nostdin", "-v", "error", "-i", str(path), "-f", "null", "-"],
        max(TIMEOUT_VIDEO_VALIDATE_MIN, duration * VIDEO_VALIDATE_SEC_PER_SEC),
        cancelled,
        on_proc,
    )
    if rc != 0 or err.strip():
        raise OutputInvalid("decode", clean_error_line(err))
