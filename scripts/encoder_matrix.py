#!/usr/bin/env python3
"""Which H.264 encoders work on this computer, how fast, and how good.

Dependency free: Python 3.9 or later and an ffmpeg, nothing to install. Runs on
Windows, Linux and macOS, also with only the StemDeck app's own ffmpeg:

    python3 scripts/encoder_matrix.py
    python3 scripts/encoder_matrix.py --ffmpeg /path/to/ffmpeg --json matrix.json

For every GPU encoder StemDeck would try on this OS (and libx264, the CPU
encoder StemDeck uses today), it runs a REAL encode at 720p, 1080p and 4K, 30
fps, of a synthetic picture that is hard to compress (testsrc2 plus moving
noise), 5 seconds each, with the exact arguments the app uses
(app/pipeline/video_encoder.py; a test keeps the two in step). Per encoder and
size it reports:

- pass or fail, and the ffmpeg error line when it fails
- encode speed (x realtime) and the output's average and peak bitrate
- picture quality against the source (SSIM and PSNR), next to libx264 at the
  SAME average bitrate, and a flag when the GPU file is more than 0.02 SSIM
  worse than libx264 at that bitrate

It prints a table and, with --json, writes everything to a file to paste into a
bug report. It never stops on a missing encoder or a failed encode.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CANDIDATES = {
    "windows": ("h264_nvenc", "h264_qsv", "h264_amf"),
    "linux": ("h264_nvenc", "h264_vaapi", "h264_qsv"),
    "macos": ("h264_videotoolbox",),
}
CPU = "libx264"
SIZES = {"720p": (1280, 720), "1080p": (1920, 1080), "4K": (3840, 2160)}
FPS = 30
SSIM_DROP_FLAG = 0.02
TIMEOUT_ENCODE = 300
TIMEOUT_SMALL = 20

# --- The app's encoder arguments (app/pipeline/video_encoder.py) -------------

GPU_MAX_BITS_PER_PIXEL = 0.5
_LEVELS = (
    ("3.1", 3600, 108000, 17500),
    ("3.2", 5120, 216000, 25000),
    ("4.0", 8192, 245760, 25000),
    ("4.1", 8192, 245760, 62500),
    ("4.2", 8704, 522240, 62500),
    ("5.0", 22080, 589824, 168750),
    ("5.1", 36864, 983040, 300000),
    ("5.2", 36864, 2073600, 300000),
)


def max_bitrate_kbps(width, height, fps):
    return int(width * height * fps * GPU_MAX_BITS_PER_PIXEL / 1000)


def h264_level(width, height, fps, kbps=None):
    mbs = ((width + 15) // 16) * ((height + 15) // 16)
    rate = kbps if kbps is not None else max_bitrate_kbps(width, height, fps)
    for name, frame_mbs, mbps, max_kbps in _LEVELS:
        if mbs <= frame_mbs and mbs * fps <= mbps and rate <= max_kbps:
            return name
    return _LEVELS[-1][0]


def variant_count(encoder):
    return 2 if encoder in ("h264_videotoolbox", "h264_nvenc") else 1


def encoder_args(encoder, width, height, fps, variant=0):
    kbps = max_bitrate_kbps(width, height, fps)
    level = h264_level(width, height, fps, kbps)
    cap = ["-maxrate", f"{kbps}k", "-bufsize", f"{2 * kbps}k"]
    if encoder == "h264_nvenc":
        rc = ["-preset", "p4", "-tune", "hq"] if variant == 0 else ["-preset", "slow"]
        return ["-c:v", "h264_nvenc", *rc, "-rc", "vbr", "-cq", "19", "-b:v", "0", *cap,
                "-profile:v", "high", "-level", level, "-pix_fmt", "yuv420p"]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "21", "-look_ahead", "0",
                "-profile:v", "high", "-pix_fmt", "nv12"]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22",
                "-qp_b", "24", "-profile:v", "high", "-pix_fmt", "nv12"]
    if encoder == "h264_vaapi":
        return ["-c:v", "h264_vaapi", "-rc_mode", "CQP", "-global_quality", "21", "-profile:v", "high"]
    if encoder == "h264_videotoolbox":
        quality = ["-q:v", "65"] if variant == 0 else ["-b:v", f"{kbps // 3}k", *cap]
        return ["-c:v", "h264_videotoolbox", *quality, "-profile:v", "high", "-pix_fmt", "yuv420p"]
    raise ValueError(encoder)


def libx264_args(width, height):
    """The app's CPU encode (app/pipeline/take_render.libx264_args)."""
    return ["-c:v", "libx264", *(["-preset", "veryfast"] if min(width, height) >= 2160 else []),
            "-crf", "20", "-pix_fmt", "yuv420p"]


def vaapi_device():
    nodes = sorted(Path("/dev/dri").glob("renderD*")) if os.name == "posix" else []
    return str(nodes[0]) if nodes else None


def global_args(encoder):
    if encoder == "h264_vaapi":
        dev = vaapi_device()
        return ["-vaapi_device", dev] if dev else []
    return []


def output_filter(encoder):
    return "format=nv12,hwupload" if encoder == "h264_vaapi" else "format=yuv420p"


# --- Helpers --------------------------------------------------------------------


def os_key():
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def run(cmd, timeout=TIMEOUT_SMALL):
    """(returncode, stdout, stderr, seconds); never raises."""
    start = time.monotonic()
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, p.stdout, p.stderr, time.monotonic() - start
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout} s", time.monotonic() - start
    except OSError as exc:
        return -2, "", f"could not run: {exc}", time.monotonic() - start


_NOISE = re.compile(
    r"could not open encoder before eof|task finished with error|terminating thread|nothing was written|"
    r"error while filtering|error opening output|error selecting|conversion failed|^\s*$", re.IGNORECASE)


def error_line(stderr):
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    pick = next((ln for ln in lines if not _NOISE.search(ln)), lines[0] if lines else "")
    pick = re.sub(r"0x[0-9a-fA-F]+", "", re.sub(r"^(\[[^\]]*\]\s*)+", "", pick))
    return pick[:200]


def find_ffmpeg(explicit):
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    tries = []
    if explicit:
        # The one asked for, or none: never quietly test another ffmpeg.
        found = Path(explicit) if Path(explicit).is_file() else shutil.which(explicit)
        return str(found) if found else None
    if os.environ.get("STEMDECK_FFMPEG"):
        tries.append(Path(os.environ["STEMDECK_FFMPEG"]))
    if os.environ.get("STEMDECK_FFMPEG_DIR"):
        tries.append(Path(os.environ["STEMDECK_FFMPEG_DIR"]) / exe)
    home = Path.home()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        data = Path(os.environ["LOCALAPPDATA"]) / "StemDeck"
    elif sys.platform == "darwin":
        data = home / "Library" / "Application Support" / "StemDeck"
    else:
        data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "stemdeck"
    tries += [data / "ffmpeg" / exe, data / "ffmpeg" / "bin" / exe]
    for path in tries:
        if path.is_file():
            return str(path)
    return shutil.which("ffmpeg")


def gpu_info():
    """Whatever the system says about its graphics hardware, cheaply. Every
    source is optional; a missing tool is skipped."""
    info = {}
    if shutil.which("nvidia-smi"):
        rc, out, _e, _s = run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                               "--format=csv,noheader"])
        if rc == 0:
            info["nvidia-smi"] = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if sys.platform == "darwin" and shutil.which("system_profiler"):
        rc, out, _e, _s = run(["system_profiler", "SPDisplaysDataType"], timeout=30)
        if rc == 0:
            info["system_profiler"] = [ln.strip() for ln in out.splitlines()
                                       if re.search(r"Chipset Model|Metal|Vendor|Total Number of Cores", ln)]
        rc, out, _e, _s = run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if rc == 0:
            info["cpu"] = out.strip()
    if sys.platform.startswith("linux"):
        if shutil.which("lspci"):
            rc, out, _e, _s = run(["lspci"])
            if rc == 0:
                info["lspci"] = [ln for ln in out.splitlines() if re.search(r"VGA|3D|Display", ln)]
        info["dri"] = sorted(str(p) for p in Path("/dev/dri").glob("*")) if Path("/dev/dri").exists() else []
    if os.name == "nt":
        ps = shutil.which("powershell")
        if ps:
            rc, out, _e, _s = run([ps, "-NoProfile", "-Command",
                                   "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + ' | ' + $_.DriverVersion }"],
                                  timeout=30)
            if rc == 0:
                info["video_controllers"] = [ln.strip() for ln in out.splitlines() if ln.strip()]
        elif shutil.which("wmic"):
            rc, out, _e, _s = run(["wmic", "path", "win32_VideoController", "get", "name"])
            if rc == 0:
                info["video_controllers"] = [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]
    return info


def source(width, height, seconds):
    """A moving test picture with temporal noise: deterministic (fixed seed),
    so the same graph is the reference for SSIM/PSNR."""
    return (f"testsrc2=s={width}x{height}:r={FPS}:d={seconds},"
            f"noise=alls=10:allf=t:all_seed=4242,format=yuv420p")


def bitrates(ffprobe, path, seconds):
    """(average kbps of the video stream, peak kbps over any 1 s window)."""
    rc, out, _e, _s = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "packet=pts_time,size", "-of", "csv=p=0", str(path)], timeout=60)
    if rc != 0:
        return None, None
    pkts = []
    for ln in out.splitlines():
        parts = ln.split(",")
        try:
            pkts.append((float(parts[0]), int(parts[1])))
        except (ValueError, IndexError):
            continue
    if not pkts:
        return None, None
    total = sum(size for _t, size in pkts)
    buckets = {}
    for t, size in pkts:
        buckets[int(t)] = buckets.get(int(t), 0) + size
    return round(total * 8 / seconds / 1000), round(max(buckets.values()) * 8 / 1000)


def quality(ffmpeg, path, width, height, seconds):
    """(SSIM, PSNR) of the encoded file against the regenerated source."""
    rc, _o, err, _s = run([
        ffmpeg, "-nostdin", "-hide_banner", "-i", str(path), "-f", "lavfi", "-i", source(width, height, seconds),
        "-lavfi", "[0:v]format=yuv420p,split[a1][a2];[1:v]split[b1][b2];[a1][b1]ssim;[a2][b2]psnr",
        "-f", "null", "-"], timeout=TIMEOUT_ENCODE)
    if rc != 0:
        return None, None
    s = re.search(r"SSIM .*All:([\d.]+)", err)
    p = re.search(r"PSNR .*average:([\d.]+|inf)", err)
    return (float(s.group(1)) if s else None,
            (float(p.group(1)) if p and p.group(1) != "inf" else None))


def encode(ffmpeg, encoder, args, width, height, seconds, out):
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", *global_args(encoder),
           "-f", "lavfi", "-i", source(width, height, seconds), "-vf", output_filter(encoder),
           *args, "-r", str(FPS), "-an", "-y", str(out)]
    rc, _o, err, wall = run(cmd, timeout=TIMEOUT_ENCODE)
    ok = rc == 0 and out.is_file() and out.stat().st_size > 0
    return ok, wall, ("" if ok else error_line(err) or f"exit {rc}")


def measure(ffmpeg, ffprobe, encoder, size_name, seconds, work):
    width, height = SIZES[size_name]
    row = {"encoder": encoder, "size": size_name, "ok": False}
    try:
        if encoder == "h264_vaapi" and vaapi_device() is None:
            row["error"] = "no /dev/dri render device"
            return row
        out = work / f"{encoder}_{size_name}.mp4"
        if encoder == CPU:
            ok, wall, err = encode(ffmpeg, CPU, libx264_args(width, height), width, height, seconds, out)
            row["args"] = "app (crf 20" + (", veryfast)" if min(width, height) >= 2160 else ")")
        else:
            ok, wall, err = False, 0.0, ""
            for variant in range(variant_count(encoder)):
                ok, wall, err = encode(ffmpeg, encoder, encoder_args(encoder, width, height, FPS, variant),
                                       width, height, seconds, out)
                row["variant"] = variant
                if ok or "Unknown encoder" in err:
                    break
        if not ok:
            row["error"] = err
            return row
        row["ok"] = True
        row["speed"] = round(seconds / wall, 2)
        row["avg_kbps"], row["peak_kbps"] = bitrates(ffprobe, out, seconds)
        row["ssim"], row["psnr"] = quality(ffmpeg, out, width, height, seconds)
        if encoder != CPU and row["avg_kbps"]:
            # libx264 at the same average bitrate, with the app's preset for this size.
            kbps = row["avg_kbps"]
            ref = work / f"x264_matched_{encoder}_{size_name}.mp4"
            preset = ["-preset", "veryfast"] if min(width, height) >= 2160 else []
            ok2, _w, err2 = encode(ffmpeg, CPU, ["-c:v", "libx264", *preset, "-b:v", f"{kbps}k",
                                                 "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k",
                                                 "-pix_fmt", "yuv420p"], width, height, seconds, ref)
            if ok2:
                row["x264_same_kbps"], _peak = bitrates(ffprobe, ref, seconds)
                row["x264_ssim"], row["x264_psnr"] = quality(ffmpeg, ref, width, height, seconds)
                if row["ssim"] is not None and row["x264_ssim"] is not None:
                    row["ssim_drop"] = round(row["x264_ssim"] - row["ssim"], 4)
                    row["flag"] = row["ssim_drop"] > SSIM_DROP_FLAG
            else:
                row["x264_error"] = err2
            ref.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
    except Exception as exc:  # never stop the matrix
        row["ok"] = False
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def table(rows):
    head = ["encoder", "size", "result", "speed", "avg kbps", "peak kbps", "SSIM", "PSNR",
            "x264 SSIM@same", "drop", "note"]
    lines = []
    for r in rows:
        if r.get("ok"):
            lines.append([r["encoder"], r["size"], "pass", f"{r.get('speed', 0):.2f}x",
                          str(r.get("avg_kbps") or "?"), str(r.get("peak_kbps") or "?"),
                          f"{r['ssim']:.4f}" if r.get("ssim") is not None else "?",
                          f"{r['psnr']:.2f}" if r.get("psnr") is not None else "?",
                          f"{r['x264_ssim']:.4f}" if r.get("x264_ssim") is not None else "",
                          f"{r['ssim_drop']:+.4f}" if r.get("ssim_drop") is not None else "",
                          "QUALITY DROP" if r.get("flag") else (r.get("args") or "")])
        else:
            lines.append([r["encoder"], r["size"], "FAIL", "", "", "", "", "", "", "", r.get("error", "")[:90]])
    # Every column padded to its widest cell, except the last (free text).
    widths = [max([len(head[i])] + [len(ln[i]) for ln in lines]) if i < len(head) - 1 else 0
              for i in range(len(head))]

    def fmt(cols):
        return "  ".join(c.ljust(widths[i]) if widths[i] else c for i, c in enumerate(cols)).rstrip()

    return "\n".join([fmt(head), fmt(["-" * w if w else "----" for w in widths]), *map(fmt, lines)])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ffmpeg", help="ffmpeg to test (default: the app's, then PATH)")
    ap.add_argument("--json", help="write the full result here")
    ap.add_argument("--seconds", type=float, default=5.0, help="length of each test encode (5)")
    ap.add_argument("--sizes", default="720p,1080p,4K", help="comma list of 720p,1080p,4K")
    ap.add_argument("--encoders", help="comma list instead of this OS's candidates")
    args = ap.parse_args(argv)

    ffmpeg = find_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        print("No ffmpeg found. Pass --ffmpeg /path/to/ffmpeg.")
        return 2
    probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    ffprobe = str(Path(ffmpeg).with_name(probe_name))
    if not Path(ffprobe).is_file():
        ffprobe = shutil.which("ffprobe") or probe_name
    rc, out, _e, _s = run([ffmpeg, "-hide_banner", "-version"])
    version = out.splitlines()[0] if rc == 0 and out else "unknown"
    config = next((ln for ln in out.splitlines() if ln.startswith("configuration:")), "")
    rc, enc_out, _e, _s = run([ffmpeg, "-hide_banner", "-encoders"])
    listed = sorted(set(re.findall(r"^\s*V\S*\s+(h264_\w+|libx264)\b", enc_out, re.MULTILINE)))
    key = os_key()
    encoders = [e.strip() for e in args.encoders.split(",")] if args.encoders else list(CANDIDATES[key])
    sizes = [s.strip() for s in args.sizes.split(",") if s.strip() in SIZES]

    report = {
        "script": "scripts/encoder_matrix.py",
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "os": key,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "ffmpeg": ffmpeg,
        "ffmpeg_version": version,
        "ffmpeg_configuration": config[:2000],
        "h264_encoders_listed": listed,
        "gpu": {},
        "seconds": args.seconds,
        "rows": [],
    }
    try:
        report["gpu"] = gpu_info()
    except Exception as exc:  # optional information only
        report["gpu"] = {"error": f"{type(exc).__name__}: {exc}"}
    print(f"ffmpeg:   {version}\n          {ffmpeg}")
    print(f"system:   {report['platform']} ({report['machine']})")
    for name, value in report["gpu"].items():
        print(f"gpu:      {name}: {value}")
    print(f"listed:   {', '.join(listed) or 'no H.264 encoders'}")
    print(f"testing:  {', '.join([*encoders, CPU])} at {', '.join(sizes)}, {FPS} fps, {args.seconds:g} s each\n")

    with tempfile.TemporaryDirectory(prefix="stemdeck_matrix_") as tmp:
        work = Path(tmp)
        for size in sizes:
            for encoder in [*encoders, CPU]:
                row = measure(ffmpeg, ffprobe, encoder, size, args.seconds, work)
                report["rows"].append(row)
                status = (f"pass {row.get('speed', 0):.2f}x" if row.get("ok")
                          else f"FAIL {row.get('error', '')[:70]}")
                print(f"  {encoder:18} {size:6} {status}", flush=True)
    print()
    print(table(report["rows"]))
    flagged = [f"{r['encoder']} {r['size']}" for r in report["rows"] if r.get("flag")]
    print()
    print(f"SSIM flag (more than {SSIM_DROP_FLAG} below libx264 at the same bitrate): "
          f"{', '.join(flagged) if flagged else 'none'}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"JSON written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
