"""The GPU video encoder of the take render (app/pipeline/video_encoder.py)
and its CPU fallback (app/api/takes.py _render_task).

Almost everything runs against a FAKE ffmpeg (subprocess.Popen and
subprocess.run replaced), so no test depends on the machine's GPU. The last
tests run the real ffmpeg: a GPU attempt that cannot work (an encoder that
does not exist, a file of the wrong size) must end in a valid libx264 MP4
whose status says it fell back.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.api import stems as stems_mod
from app.api import takes as takes_mod
from app.api import video_encoders as video_encoders_api
from app.core import settings as settings_mod
from app.core.config import VIDEO_ENCODER_CHOICES, VIDEO_GPU_ENCODERS
from app.pipeline import take_render
from app.pipeline import video_encoder as ve
from app.pipeline.take_render import AudioLane, build_video_render_cmd

# ─── The fake ffmpeg ─────────────────────────────────────────────────────


class FakeProc:
    """Enough of subprocess.Popen for the probe, the validator and
    run_video_render. `hang` blocks stdout and wait() until kill()."""

    def __init__(self, cmd, rc=0, out="", err="", hang=False, timeout_now=False):
        self.args = cmd
        self._rc, self._out, self._err = rc, out, err
        self._hang = hang
        self._timeout_now = timeout_now
        self._done = threading.Event()
        self.killed = False
        self.returncode = None
        self.stdout = self._stdout()
        self.stderr = iter(err.splitlines(True))

    def _stdout(self):
        yield from self._out.splitlines(True)
        if self._hang:
            self._done.wait(30)

    def _finished(self) -> bool:
        return not self._hang or self._done.is_set()

    def poll(self):
        if not self._finished():
            return None
        if self.returncode is None:
            self.returncode = -9 if self.killed else self._rc
        return self.returncode

    def wait(self, timeout=None):
        if not self._finished() and not self._done.wait(timeout if timeout is not None else 30):
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.poll()

    def communicate(self, timeout=None):
        if self._timeout_now and not self.killed and timeout is not None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if not self._finished() and not self._done.wait(timeout if timeout is not None else 30):
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.poll()
        return self._out, self._err

    def kill(self):
        self.killed = True
        self._done.set()


GOOD_PROBE = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "r_frame_rate": "30/1",
            "duration": "10.000000",
        },
        {"codec_type": "audio", "codec_name": "aac", "duration": "10.000000"},
    ],
    "format": {"duration": "10.010000"},
}


# What a failed render prints. "noinput" and "nvenc" are ffmpeg 8.1's own
# words on Windows with an NVIDIA card (run by hand for #791); "full" is put
# together from the same ffmpeg's muxer messages and the system's text for a
# full disk, which was not reproduced.
RENDER_ERRORS = {
    "noinput": (
        "[in#1 @ 00000197cb4959c0] Error opening input: No such file or directory\n"
        "Error opening input file D:/x/stems/drums.wav.\n"
        "Error opening input files: No such file or directory\n"
    ),
    "full": (
        "[out#0/mp4 @ 0000016b35eacf00] Error muxing a packet\n"
        "[vost#0:0/h264_nvenc @ 0000016b35f3ae00] Error submitting a packet to the muxer: "
        "No space left on device\n"
        "[out#0/mp4 @ 0000016b35eacf00] Error writing trailer: No space left on device\n"
    ),
    "nvenc": (
        "[h264_nvenc @ 0000016b35f3b080] No capable devices found\n"
        "[vost#0:0/h264_nvenc @ 0000016b35f3ae00] [enc:h264_nvenc @ 0000016b35f34b80] "
        "Error while opening encoder - maybe incorrect parameters such as bit_rate, rate, "
        "width or height.\n"
        "[vf#0:0 @ 0000016b35f55f40] Error sending frames to consumers: Generic error in an "
        "external library\n"
    ),
    "busy": "[h264_nvenc @ 0x5] OpenEncodeSessionEx failed: out of memory (10)\n",
    "unknown": "[vf#0:0 @ 0x1] Something nobody has seen before\n",
}


class FakeFfmpeg:
    """Answers by what the command is: -version, a probe (testsrc2), ffprobe,
    the decode check (-f null on a file), or a render (-movflags)."""

    def __init__(self, tmp_path: Path):
        self.binary = tmp_path / "bin" / "ffmpeg"
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        self.binary.write_bytes(b"fake ffmpeg 1")
        self.version = "ffmpeg version 9.9-fake"
        self.probe = {}  # encoder -> ok | missing | fail | busy | timeout | short
        self.render = {}  # encoder -> ok | fail | hang | stall
        self.render_bytes = 200_000
        self.ffprobe = json.loads(json.dumps(GOOD_PROBE))
        self.decode = (0, "")
        self.calls: list[list[str]] = []
        self.procs: list[FakeProc] = []

    def kind(self, cmd) -> str:
        if "-version" in cmd:
            return "version"
        if cmd[0] == "ffprobe-fake":
            return "ffprobe"
        if any("testsrc2" in a for a in cmd):
            return "probe"
        if "-movflags" in cmd:
            return "render"
        return "decode"

    def encoder(self, cmd) -> str:
        return cmd[cmd.index("-c:v") + 1] if "-c:v" in cmd else ""

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if self.kind(c) == kind)

    def popen(self, cmd, **_kw) -> FakeProc:
        self.calls.append(list(cmd))
        kind = self.kind(cmd)
        if kind == "version":
            proc = FakeProc(cmd, out=f"{self.version}\nbuilt with fake\n")
        elif kind == "ffprobe":
            proc = FakeProc(cmd, out=json.dumps(self.ffprobe))
        elif kind == "decode":
            rc, err = self.decode
            proc = FakeProc(cmd, rc=rc, err=err)
        elif kind == "probe":
            enc = self.encoder(cmd)
            how = self.probe.get(enc, "ok" if enc == "libx264" else "missing")
            enc = self.encoder(cmd)
            proc = {
                "ok": lambda: FakeProc(cmd, out="frame=30\nframe=60\nprogress=end\n"),
                "short": lambda: FakeProc(cmd, out="frame=3\nprogress=end\n"),
                "missing": lambda: FakeProc(
                    cmd, rc=8, err=f"[vost#0:0 @ 0x1] Unknown encoder '{enc}'\n"
                ),
                "fail": lambda: FakeProc(
                    cmd,
                    rc=1,
                    err="[enc @ 0x5a] No capable devices found\nConversion failed!\n",
                ),
                "busy": lambda: FakeProc(
                    cmd, rc=1, err="[h264_nvenc @ 0x5] OpenEncodeSessionEx failed: out of memory\n"
                ),
                "timeout": lambda: FakeProc(cmd, hang=True, timeout_now=True),
                # Runs until killed (a slow first test of a card).
                "hang": lambda: FakeProc(cmd, hang=True),
            }[how]()
        else:  # render
            how = self.render.get(self.encoder(cmd), "ok")
            dest = Path(cmd[-1])
            if how == "ok":
                dest.write_bytes(b"\0" * self.render_bytes)
                proc = FakeProc(
                    cmd, out="out_time_us=5000000\nout_time_us=10000000\nprogress=end\n"
                )
            elif how == "fail":
                proc = FakeProc(cmd, rc=1, err="[h264_nvenc @ 0x1] Error submitting frame\n")
            elif how in RENDER_ERRORS:
                proc = FakeProc(cmd, rc=1, err=RENDER_ERRORS[how])
            elif how == "stall":
                dest.write_bytes(b"\0" * 100)
                proc = FakeProc(cmd, out="out_time_us=1000000\n", hang=True)
            else:  # hang
                proc = FakeProc(cmd, hang=True)
        self.procs.append(proc)
        return proc

    def run(self, cmd, timeout=None, **kw):
        proc = self.popen(cmd, **kw)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = FakeFfmpeg(tmp_path)
    monkeypatch.setattr(subprocess, "Popen", f.popen)
    monkeypatch.setattr(subprocess, "run", f.run)
    monkeypatch.setattr(ve, "ffmpeg_executable", lambda: str(f.binary))
    monkeypatch.setattr(ve, "ffprobe_executable", lambda: "ffprobe-fake")
    # The suite's kill switch off: these tests are about the GPU path.
    monkeypatch.setattr(ve, "VIDEO_ENCODER_OVERRIDE", "")
    monkeypatch.setattr(ve, "candidates", lambda platform=None: ("h264_nvenc", "h264_qsv"))
    return f


# ─── Candidates and arguments (pure) ─────────────────────────────────────


def test_candidates_per_os_in_order_never_mf():
    assert ve.candidates("win32") == ("h264_nvenc", "h264_qsv", "h264_amf")
    assert ve.candidates("linux") == ("h264_nvenc", "h264_vaapi", "h264_qsv")
    assert ve.candidates("darwin") == ("h264_videotoolbox",)
    for plat in ("win32", "linux", "darwin", "freebsd13"):
        assert "h264_mf" not in ve.candidates(plat)
        assert "libx264" not in ve.candidates(plat)
    assert "h264_mf" not in VIDEO_GPU_ENCODERS and "h264_mf" not in VIDEO_ENCODER_CHOICES


def test_levels_fit_size_rate_and_bitrate():
    assert ve.h264_level(1280, 720, 30) == "3.1"
    assert ve.h264_level(1920, 1080, 30) == "4.1"
    assert ve.h264_level(1920, 1080, 60) == "4.2"
    assert ve.h264_level(3840, 2160, 30) == "5.1"
    # 4K60 is blocked in the app; the table still answers 5.2 for it.
    assert ve.h264_level(3840, 2160, 60) == "5.2"
    assert ve.max_bitrate_kbps(1920, 1080, 30) == 31104


@pytest.mark.parametrize("encoder", VIDEO_GPU_ENCODERS)
def test_gpu_args_are_high_profile_constant_quality(encoder):
    for variant in range(ve.variant_count(encoder)):
        args = ve.encoder_args(encoder, 1920, 1080, 30, variant)
        assert args[:2] == ["-c:v", encoder]
        assert args[args.index("-profile:v") + 1] == "high"
        assert any(q in args for q in ("-cq", "-global_quality", "-qp_i", "-q:v", "-b:v"))
    with pytest.raises(ValueError):
        ve.encoder_args("h264_mf", 1920, 1080, 30)


def test_nvenc_args():
    args = ve.encoder_args("h264_nvenc", 3840, 2160, 30)
    joined = " ".join(args)
    assert "-preset p4 -tune hq -rc vbr -cq 19 -b:v 0" in joined
    assert "-maxrate 124416k -bufsize 248832k" in joined
    assert "-level 5.1 -pix_fmt yuv420p" in joined


def test_error_line_is_cleaned():
    err = (
        "[vost#0:0/h264_qsv @ 000001e681d30480] [enc:h264_qsv @ 0x5f] Could not open encoder before EOF\n"
        "[h264_qsv @ 0x580b2b57ec40] Error creating a MFX session: -9.\n"
        "Conversion failed!\n"
    )
    assert ve.clean_error_line(err) == "Error creating a MFX session: -9."
    assert ve.classify_failure("Unknown encoder 'h264_amf'") == ("missing", False)
    assert ve.classify_failure("OpenEncodeSessionEx failed: out of memory (10)") == ("busy", True)
    assert ve.classify_failure("Device or resource busy") == ("busy", True)
    assert ve.classify_failure("No NVENC capable devices found") == ("failed", False)


# ─── The CPU path is unchanged (golden) ──────────────────────────────────


def _golden_inputs():
    return [
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(Path("L/drums.wav")),
        "-i",
        str(Path("L/bass.wav")),
        "-i",
        str(Path("T/audio.wav")),
    ]


def test_cpu_command_is_byte_for_byte_unchanged():
    """The argument lists the render built before the GPU path existed
    (generated from take_render.py at 324f8dc), for the default 1080p30
    camera take and a 9:16 4K60 Streamer render with a master."""
    lanes = [
        AudioLane(Path("L/drums.wav"), 0.8, "", ""),
        AudioLane(Path("L/bass.wav"), 1.0, "rubberband=pitch=1.05,", "biquad=b=1,"),
    ]
    default = dict(
        lanes=lanes,
        take_audio=Path("T/audio.wav"),
        take_gain=1.0,
        camera=Path("T/camera.mp4"),
        camera_start_sec=0.25,
        duration_sec=12.5,
        canvas="16:9",
        fit="cover",
        mirror=False,
        audio_rate=44100,
    )
    golden_default = [
        *_golden_inputs(),
        "-i",
        str(Path("T/camera.mp4")),
        "-filter_complex",
        "[0:a]volume=0.800000[a0];[1:a]biquad=b=1,rubberband=pitch=1.05,volume=1.000000[a1];"
        "[2:a]volume=1.000000[a2];[a0][a1][a2]amix=inputs=3:normalize=0,aresample=176400,"
        "alimiter=limit=0.841395:level=disabled:latency=1,aresample=44100[aout];"
        "color=c=black:s=1920x1080:r=30:d=12.500000[bg];[3:v]setpts=PTS-STARTPTS+(0.250000)/TB,"
        "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1[cam];"
        "[bg][cam]overlay=eof_action=pass:shortest=0,fps=30,format=yuv420p[vout]",
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-aac_is",
        "0",
        "-ar",
        "44100",
        "-t",
        "12.500000",
        "-movflags",
        "+faststart",
    ]
    streamer = dict(
        default,
        take_gain=0.9,
        camera=Path("T/camera.webm"),
        camera_start_sec=-0.1,
        duration_sec=30.0,
        canvas="9:16",
        fit="fill",
        mirror=True,
        audio_rate=48000,
        start_sec=1.5,
        take_fx="biquad=x,",
        master_fx="biquad=m,",
        master_chain="acompressor,alimiter",
        trim_db=0.7,
        alt_aac=True,
        size="1080",
        fps=60,
    )
    golden_streamer = [
        *_golden_inputs(),
        "-i",
        str(Path("T/camera.webm")),
        "-filter_complex",
        "[0:a]atrim=start=1.500000,asetpts=PTS-STARTPTS,volume=0.800000[a0];"
        "[1:a]atrim=start=1.500000,asetpts=PTS-STARTPTS,biquad=b=1,rubberband=pitch=1.05,"
        "volume=1.000000[a1];[2:a]atrim=start=1.500000,asetpts=PTS-STARTPTS,biquad=x,"
        "volume=0.900000[a2];[a0][a1][a2]amix=inputs=3:normalize=0,biquad=m,acompressor,"
        "alimiter,volume=0.92257143[aout];color=c=black:s=1080x1920:r=60:d=30.000000[bg];"
        "[3:v]setpts=PTS-STARTPTS+(-1.600000)/TB,trim=start=0,scale=1080:1920,hflip,"
        "setsar=1[cam];[bg][cam]overlay=eof_action=pass:shortest=0,fps=60,format=yuv420p[vout]",
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "60",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-aac_is",
        "0",
        "-aac_coder",
        "fast",
        "-ar",
        "48000",
        "-t",
        "30.000000",
        "-movflags",
        "+faststart",
    ]
    for kw, golden in ((default, golden_default), (streamer, golden_streamer)):
        assert build_video_render_cmd(**kw)[1:] == golden
        assert build_video_render_cmd(**kw, encoder="libx264", encoder_variant=1)[1:] == golden


def test_gpu_command_changes_only_the_encode():
    lanes = [AudioLane(Path("L/drums.wav"), 0.8, "", "")]
    kw = dict(
        lanes=lanes,
        take_audio=Path("T/audio.wav"),
        take_gain=1.0,
        camera=Path("T/camera.mp4"),
        camera_start_sec=0.0,
        duration_sec=10.0,
        canvas="16:9",
        fit="fill",
        mirror=False,
        audio_rate=44100,
        size="1080",
        fps=30,
    )
    cpu = build_video_render_cmd(**kw)
    gpu = build_video_render_cmd(**kw, encoder="h264_nvenc")
    # Same graph and audio, same tail; only the video codec block differs.
    assert gpu[gpu.index("-filter_complex") + 1] == cpu[cpu.index("-filter_complex") + 1]
    assert gpu[gpu.index("-r") :] == cpu[cpu.index("-r") :]
    assert "libx264" not in gpu and gpu[gpu.index("-c:v") + 1] == "h264_nvenc"
    vaapi = build_video_render_cmd(**kw, encoder="h264_vaapi")
    assert "format=nv12,hwupload[vout]" in vaapi[vaapi.index("-filter_complex") + 1]
    with pytest.raises(ValueError):
        build_video_render_cmd(**kw, encoder="h264_mf")
    with pytest.raises(ValueError):
        build_video_render_cmd(**kw, encoder="libx264; rm -rf /")


# ─── Probe and its cache ─────────────────────────────────────────────────


def test_probe_pass_fail_timeout(fake):
    fake.probe = {"h264_nvenc": "ok", "h264_qsv": "fail", "h264_amf": "timeout"}
    ok = ve.probe("h264_nvenc", 3840, 2160, 30)
    assert ok.ok and ok.speed and ok.speed > 0
    probe_cmd = next(c for c in fake.calls if fake.kind(c) == "probe")
    assert "testsrc2=s=3840x2160:r=30:d=2" in probe_cmd, "probed at the render's own geometry"
    bad = ve.probe("h264_qsv", 1920, 1080, 30)
    assert not bad.ok and bad.reason == "failed" and not bad.transient
    assert bad.detail == "No capable devices found"
    slow = ve.probe("h264_amf", 1920, 1080, 30)
    assert not slow.ok and slow.reason == "timeout" and slow.transient
    assert fake.procs[-1].killed, "a probe that times out is killed"
    with pytest.raises(ValueError):
        ve.probe("libx264", 1920, 1080, 30)


def test_probe_counts_frames(fake):
    fake.probe = {"h264_nvenc": "short"}
    r = ve.probe("h264_nvenc", 1920, 1080, 30)
    assert not r.ok and r.reason == "failed" and "of 60 frames" in r.detail


def test_probe_is_cached_in_memory_and_on_disk(fake):
    fake.probe = {"h264_nvenc": "ok"}
    ve.probe("h264_nvenc", 1920, 1080, 30)
    ve.probe("h264_nvenc", 1920, 1080, 30)
    assert fake.count("probe") == 1
    # Another geometry is another probe (4K can fail where 1080p works).
    ve.probe("h264_nvenc", 3840, 2160, 30)
    assert fake.count("probe") == 2
    saved = json.loads(ve._cache_path.read_text(encoding="utf-8"))
    assert len(saved["entries"]) == 2 and saved["v"] == 1
    # A new process (memory cleared) reads the file instead of probing.
    ve._cache.clear()
    ve._cache_loaded_for = None
    assert ve.probe("h264_nvenc", 1920, 1080, 30).ok
    assert fake.count("probe") == 2
    # A corrupt file is ignored, never fatal.
    ve._cache_path.write_text("{not json", encoding="utf-8")
    ve._cache.clear()
    ve._cache_loaded_for = None
    assert ve.probe("h264_nvenc", 1920, 1080, 30).ok
    assert fake.count("probe") == 3


def test_a_new_ffmpeg_invalidates_the_cache(fake):
    fake.probe = {"h264_nvenc": "ok"}
    ve.probe("h264_nvenc", 1920, 1080, 30)
    # Another binary (the setup replaced it): new size, so a new identity.
    fake.binary.write_bytes(b"fake ffmpeg 2, a different build")
    ve.probe("h264_nvenc", 1920, 1080, 30)
    assert fake.count("probe") == 2
    # The same file reporting another version is another identity too.
    ve.reset_state()
    fake.version = "ffmpeg version 10.0-fake"
    ve.probe("h264_nvenc", 1920, 1080, 30)
    assert fake.count("probe") == 3


def test_timeouts_and_busy_devices_are_never_cached_for_long(fake, monkeypatch):
    fake.probe = {"h264_nvenc": "timeout", "h264_qsv": "fail"}
    now = time.time()
    clock = [now]
    monkeypatch.setattr(ve.time, "time", lambda: clock[0])
    assert ve.probe("h264_nvenc", 1920, 1080, 30).reason == "timeout"
    assert ve.probe("h264_qsv", 1920, 1080, 30).reason == "failed"
    clock[0] = now + 60
    ve.probe("h264_nvenc", 1920, 1080, 30)
    ve.probe("h264_qsv", 1920, 1080, 30)
    assert fake.count("probe") == 2, "both still cached a minute later"
    clock[0] = now + ve.VIDEO_ENCODER_RETRY_SEC + 1
    ve.probe("h264_nvenc", 1920, 1080, 30)
    ve.probe("h264_qsv", 1920, 1080, 30)
    assert fake.count("probe") == 3, "the timeout is tried again, the failure is not"
    clock[0] = now + ve.VIDEO_ENCODER_FAIL_TTL_SEC + 1
    ve.probe("h264_qsv", 1920, 1080, 30)
    assert fake.count("probe") == 4, "a failure is tried again the next day"
    fake.probe = {"h264_nvenc": "busy"}
    clock[0] += ve.VIDEO_ENCODER_RETRY_SEC + 1
    busy = ve.probe("h264_nvenc", 1920, 1080, 30)
    assert busy.reason == "busy" and busy.transient


def test_changed_arguments_are_probed_again(fake, monkeypatch):
    """A release that changes an encoder's arguments never trusts a pass of
    the old ones, even within the week a pass is kept."""
    fake.probe = {"h264_nvenc": "ok"}
    ve.probe("h264_nvenc", 1920, 1080, 30)
    real = ve.encoder_args
    monkeypatch.setattr(ve, "encoder_args", lambda *a, **k: [*real(*a, **k), "-bf", "2"])
    ve.probe("h264_nvenc", 1920, 1080, 30)
    assert fake.count("probe") == 2


def test_force_probes_again(fake):
    fake.probe = {"h264_nvenc": "ok"}
    ve.probe("h264_nvenc", 1920, 1080, 30)
    ve.probe("h264_nvenc", 1920, 1080, 30, force=True)
    assert fake.count("probe") == 2


def test_no_ffmpeg_is_a_transient_error(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(ve, "ffmpeg_executable", lambda: str(tmp_path / "nowhere" / "ffmpeg"))
    r = ve.probe("h264_nvenc", 1920, 1080, 30)
    assert not r.ok and r.reason == "error" and r.transient
    assert ve.choose(1920, 1080, 30).encoder == "libx264"


# ─── Policy ──────────────────────────────────────────────────────────────


def test_auto_takes_the_first_candidate_that_passes(fake):
    fake.probe = {"h264_nvenc": "fail", "h264_qsv": "ok"}
    assert settings_mod.get_video_encoder() == "auto"
    assert ve.choose(1920, 1080, 30).encoder == "h264_qsv"
    fake.probe = {}
    ve.reset_state()
    ve._cache_path.unlink()
    c = ve.choose(1920, 1080, 30)
    assert c.encoder == "libx264" and c.reason == "missing"


def test_setting_cpu_never_probes(fake):
    fake.probe = {"h264_nvenc": "ok"}
    settings_mod.set_video_encoder("cpu")
    c = ve.choose(1920, 1080, 30)
    assert c.encoder == "libx264" and c.reason == "policy"
    assert fake.count("probe") == 0


def test_setting_one_encoder_tries_only_that_one(fake):
    fake.probe = {"h264_nvenc": "ok", "h264_qsv": "fail"}
    settings_mod.set_video_encoder("h264_qsv")
    assert ve.choose(1920, 1080, 30).encoder == "libx264"
    probed = {fake.encoder(c) for c in fake.calls if fake.kind(c) == "probe"}
    assert probed == {"h264_qsv"}
    settings_mod.set_video_encoder("H264_NVENC ")
    assert settings_mod.get_video_encoder() == "h264_nvenc"
    assert ve.choose(1920, 1080, 30).encoder == "h264_nvenc"


def test_env_kill_switch_wins_over_the_setting(fake, monkeypatch):
    fake.probe = {"h264_nvenc": "ok"}
    settings_mod.set_video_encoder("h264_nvenc")
    monkeypatch.setattr(ve, "VIDEO_ENCODER_OVERRIDE", "cpu")
    assert ve.policy() == ("cpu", "env")
    assert ve.choose(1920, 1080, 30).encoder == "libx264"
    assert fake.count("probe") == 0


def test_env_override_is_parsed_from_a_whitelist(monkeypatch):
    from app.core import config

    monkeypatch.setenv("STEMDECK_VIDEO_ENCODER", " CPU ")
    assert config._env_choice("STEMDECK_VIDEO_ENCODER", VIDEO_ENCODER_CHOICES) == "cpu"
    monkeypatch.setenv("STEMDECK_VIDEO_ENCODER", "h264_mf")
    assert config._env_choice("STEMDECK_VIDEO_ENCODER", VIDEO_ENCODER_CHOICES) == ""
    monkeypatch.delenv("STEMDECK_VIDEO_ENCODER")
    assert config._env_choice("STEMDECK_VIDEO_ENCODER", VIDEO_ENCODER_CHOICES) == ""


def test_setting_whitelist():
    for bad in ("h264_mf", "libx264", "nvenc", "", "auto; rm -rf /", "../h264_nvenc"):
        with pytest.raises(ValueError):
            settings_mod.set_video_encoder(bad)
    assert settings_mod.get_video_encoder() == "auto"


def test_two_failures_disable_an_encoder(fake):
    fake.probe = {"h264_nvenc": "ok", "h264_qsv": "ok"}
    assert not ve.record_failure("h264_nvenc", "exit")
    assert ve.choose(1920, 1080, 30).encoder == "h264_nvenc", "one failure is not enough"
    assert ve.record_failure("h264_nvenc", "invalid-size")
    assert ve.disabled() == ["h264_nvenc"]
    assert ve.choose(1920, 1080, 30).encoder == "h264_qsv"
    assert not ve.record_failure("libx264", "exit"), "the CPU encoder is never disabled"


# ─── Validation ──────────────────────────────────────────────────────────


def _file(tmp_path, size=200_000) -> Path:
    p = tmp_path / "out.mp4"
    p.write_bytes(b"\0" * size)
    return p


def test_validate_passes_a_good_file(fake, tmp_path):
    ve.validate_output(_file(tmp_path), 1920, 1080, 30, 10.0, "h264_nvenc")
    decode = next(c for c in fake.calls if fake.kind(c) == "decode")
    assert decode[-3:] == ["-f", "null", "-"] and "-v" in decode


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda p: p["streams"][0].update(width=1280, height=720), "size"),
        (lambda p: p["streams"][0].update(codec_name="hevc"), "codec"),
        (lambda p: p["streams"][0].update(r_frame_rate="25/1"), "fps"),
        (lambda p: p["streams"].pop(1), "audio"),
        (lambda p: p["streams"][0].update(duration="6.0"), "duration"),
        (lambda p: p["streams"].pop(0), "codec"),
    ],
)
def test_validate_catches_a_wrong_file(fake, tmp_path, change, code):
    change(fake.ffprobe)
    with pytest.raises(ve.OutputInvalid) as exc:
        ve.validate_output(_file(tmp_path), 1920, 1080, 30, 10.0, "h264_nvenc")
    assert exc.value.code == code


def test_validate_catches_decode_errors_and_odd_sizes(fake, tmp_path):
    fake.decode = (0, "[h264 @ 0x1] error while decoding MB 12 4\n")
    with pytest.raises(ve.OutputInvalid) as exc:
        ve.validate_output(_file(tmp_path), 1920, 1080, 30, 10.0, "h264_nvenc")
    assert exc.value.code == "decode"
    fake.decode = (1, "")
    with pytest.raises(ve.OutputInvalid):
        ve.validate_output(_file(tmp_path), 1920, 1080, 30, 10.0, "h264_nvenc")
    fake.decode = (0, "")
    with pytest.raises(ve.OutputInvalid) as exc:
        ve.validate_output(_file(tmp_path, 100), 1920, 1080, 30, 10.0, "h264_nvenc")
    assert exc.value.code == "filesize"
    huge = ve.size_cap_bytes("h264_nvenc", 1920, 1080, 30, 1.0) + 1
    with pytest.raises(ve.OutputInvalid) as exc:
        ve.validate_output(_file(tmp_path, huge), 1920, 1080, 30, 1.0, "h264_nvenc")
    assert exc.value.code == "filesize"


# ─── The render: GPU attempt and CPU fallback ────────────────────────────


def _build(trim, alt, encoder, variant):
    return ["ffmpeg", "-i", "x", "-c:v", encoder, "-v", str(variant), "-movflags", "+faststart"]


async def _render(tmp_path, monkeypatch, retry=None, wait_for=None):
    job = SimpleNamespace(id="abcdefabcdef")
    monkeypatch.setattr(takes_mod, "registry_get", lambda jid: job)
    run = takes_mod._RenderRun()
    run.gpu = takes_mod._GpuPlan(_build, 1920, 1080, 30)
    dest = tmp_path / "render.mp4"
    cmd = _build(0.0, False, "libx264", 0)
    task = asyncio.create_task(takes_mod._render_task(run, job, cmd, dest, 10.0, retry))
    if wait_for is not None:
        await wait_for(run)
    await asyncio.wait_for(task, 30)
    return run, dest


def _renders(fake) -> list[str]:
    return [fake.encoder(c) for c in fake.calls if fake.kind(c) == "render"]


async def test_gpu_render_that_passes_is_kept(fake, tmp_path, monkeypatch):
    fake.probe = {"h264_nvenc": "ok"}
    run, dest = await _render(tmp_path, monkeypatch)
    assert run.status == "done" and dest.is_file()
    assert run.encoder == "h264_nvenc" and run.fallback is None
    assert _renders(fake) == ["h264_nvenc"]
    view = takes_mod._render_view("abcdefabcdef", "t00000000", tmp_path)
    assert view["encoder"] is None, "the view reads the registered run"
    takes_mod._renders[("abcdefabcdef", "t00000000")] = run
    try:
        view = takes_mod._render_view("abcdefabcdef", "t00000000", tmp_path)
        assert view["encoder"] == "h264_nvenc" and view["fallback"] is False
    finally:
        takes_mod._renders.clear()


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda f: f.render.update(h264_nvenc="fail"), "exit"),
        (lambda f: f.ffprobe["streams"][0].update(width=1280, height=720), "invalid-size"),
        (lambda f: f.ffprobe["streams"].pop(1), "invalid-audio"),
        (lambda f: f.ffprobe["streams"][0].update(duration="3.0"), "invalid-duration"),
        (lambda f: setattr(f, "decode", (0, "corrupt frame\n")), "invalid-decode"),
    ],
)
async def test_any_gpu_failure_renders_again_on_the_cpu(fake, tmp_path, monkeypatch, setup, reason):
    fake.probe = {"h264_nvenc": "ok"}
    setup(fake)
    run, dest = await _render(tmp_path, monkeypatch)
    assert run.status == "done", run.error
    assert run.encoder == "libx264" and run.fallback == reason
    assert _renders(fake) == ["h264_nvenc", "libx264"]
    cpu = [c for c in fake.calls if fake.kind(c) == "render"][-1]
    assert cpu[: len(_build(0, False, "libx264", 0))] == _build(0.0, False, "libx264", 0)
    assert dest.is_file()
    assert not list(tmp_path.glob(".render.mp4.*")), "the GPU file is gone"
    assert ve.failure_count("h264_nvenc") == 1
    takes_mod._renders[("abcdefabcdef", "t00000000")] = run
    try:
        view = takes_mod._render_view("abcdefabcdef", "t00000000", tmp_path)
        assert view["fallback"] is True and view["fallback_reason"] == reason
        assert view["encoder"] == "libx264"
    finally:
        takes_mod._renders.clear()


async def test_two_failed_renders_disable_the_encoder(fake, tmp_path, monkeypatch):
    fake.probe = {"h264_nvenc": "ok"}
    fake.render = {"h264_nvenc": "fail"}
    for _ in range(2):
        run, _dest = await _render(tmp_path, monkeypatch)
        assert run.fallback == "exit"
    run, _dest = await _render(tmp_path, monkeypatch)
    assert run.encoder == "libx264" and run.fallback is None
    assert _renders(fake) == ["h264_nvenc", "libx264", "h264_nvenc", "libx264", "libx264"]


@pytest.mark.parametrize(
    ("code", "err", "blamed"),
    [
        ("exit", RENDER_ERRORS["nvenc"], True),
        ("exit", "[h264_nvenc @ 0x1] Error submitting frame\n", True),
        ("exit", RENDER_ERRORS["noinput"], False),
        ("exit", RENDER_ERRORS["full"], False),
        # Constructed: the encoder complains too, but the disk is full.
        (
            "exit",
            "[h264_nvenc @ 0x1] Error submitting frame\n"
            "[out#0/mp4 @ 0x2] Error writing trailer: No space left on device\n",
            False,
        ),
        ("exit", RENDER_ERRORS["busy"], False),
        # Unknown words, from no encoder: not held against it (#791).
        ("exit", RENDER_ERRORS["unknown"], False),
        ("exit", "", False),
        # Another encoder's line says nothing about this one.
        ("exit", "[h264_qsv @ 0x1] Error creating a MFX session: -9.\n", False),
        # The file was made but is wrong, or the encoder hung: as before.
        ("stall", "", True),
        ("timeout", "", True),
        ("invalid-size", "", True),
        ("invalid-decode", "", True),
    ],
)
def test_only_encoder_faults_are_held_against_the_encoder(code, err, blamed):
    assert ve.blames_encoder("h264_nvenc", code, err) is blamed


@pytest.mark.parametrize("how", ["full", "noinput", "busy", "unknown"])
async def test_a_disk_or_input_fault_never_disables_the_encoder(fake, tmp_path, monkeypatch, how):
    # Issue #791: a full disk or a missing lane file failed two GPU renders,
    # and the encoder was off until StemDeck restarted.
    fake.probe = {"h264_nvenc": "ok"}
    fake.render = {"h264_nvenc": how}
    for _ in range(3):
        run, _dest = await _render(tmp_path, monkeypatch)
        assert run.encoder == "libx264" and run.fallback == "exit"
    assert ve.failure_count("h264_nvenc") == 0
    assert ve.disabled() == []
    assert _renders(fake) == ["h264_nvenc", "libx264"] * 3, "the GPU is still tried"


def test_a_transient_test_failure_keeps_the_earlier_pass(fake):
    # Issue #791: Test encoders while the card is busy replaced a week-long
    # pass with "out of memory" for ten minutes.
    fake.probe = {"h264_nvenc": "ok"}
    assert ve.probe("h264_nvenc", 1920, 1080, 30).ok
    fake.probe = {"h264_nvenc": "busy"}
    busy = ve.probe("h264_nvenc", 1920, 1080, 30, force=True)
    assert busy.reason == "busy" and busy.transient, "the test reports what it saw"
    probes = fake.count("probe")
    assert ve.probe("h264_nvenc", 1920, 1080, 30).ok, "renders still use the pass"
    assert fake.count("probe") == probes, "no new probe for the render"
    ve._cache.clear()
    ve._cache_loaded_for = None
    assert ve.probe("h264_nvenc", 1920, 1080, 30).ok, "the pass is kept on disk too"
    assert ve.cached_results()[0].ok
    fake.probe = {"h264_nvenc": "timeout"}
    assert ve.probe("h264_nvenc", 1920, 1080, 30, force=True).reason == "timeout"
    assert ve.probe("h264_nvenc", 1920, 1080, 30).ok


def test_a_lasting_test_failure_still_replaces_a_pass(fake):
    # A driver removed since the pass: Test encoders must be able to say so.
    fake.probe = {"h264_nvenc": "ok"}
    ve.probe("h264_nvenc", 1920, 1080, 30)
    fake.probe = {"h264_nvenc": "fail"}
    assert not ve.probe("h264_nvenc", 1920, 1080, 30, force=True).ok
    assert ve.probe("h264_nvenc", 1920, 1080, 30).reason == "failed"


def test_a_cached_pass_does_not_wait_for_a_running_test(fake):
    # Issue #791: Test encoders holds the probe lock for minutes; a render
    # (or the Settings page) that only reads the cache must not wait for it.
    fake.probe = {"h264_nvenc": "ok"}
    ve.probe("h264_nvenc", 1920, 1080, 30)
    ve._probe_lock.acquire()
    try:
        out: list = []
        reader = threading.Thread(
            target=lambda: out.append(
                (ve.probe("h264_nvenc", 1920, 1080, 30), ve.cached_results())
            ),
            daemon=True,
        )
        reader.start()
        reader.join(2)
        assert out, "the cached answer came while the lock was held"
        assert out[0][0].ok and out[0][1][0].ok
    finally:
        ve._probe_lock.release()
    assert fake.count("probe") == 1


async def test_a_stalled_gpu_render_falls_back(fake, tmp_path, monkeypatch):
    fake.probe = {"h264_nvenc": "ok"}
    fake.render = {"h264_nvenc": "stall"}
    monkeypatch.setattr(takes_mod, "TIMEOUT_VIDEO_GPU_STALL", 1)
    run, dest = await _render(tmp_path, monkeypatch)
    assert run.fallback == "stall" and run.encoder == "libx264" and dest.is_file()
    assert fake.procs[[fake.kind(c) for c in fake.calls].index("render")].killed


async def test_cancel_during_a_gpu_render_kills_it_and_does_not_fall_back(
    fake, tmp_path, monkeypatch
):
    fake.probe = {"h264_nvenc": "ok"}
    fake.render = {"h264_nvenc": "hang"}

    async def cancel_when_running(run):
        for _ in range(100):
            if run.proc is not None:
                break
            await asyncio.sleep(0.05)
        takes_mod._stop_run(run)

    run, dest = await _render(tmp_path, monkeypatch, wait_for=cancel_when_running)
    assert run.status == "none"
    gpu_proc = fake.procs[[fake.kind(c) for c in fake.calls].index("render")]
    assert gpu_proc.killed
    assert _renders(fake) == ["h264_nvenc"], "no CPU render after a cancel"
    assert ve.failure_count("h264_nvenc") == 0 and not dest.exists()


async def _cancel_after(run, delay=0.3):
    await asyncio.sleep(delay)
    takes_mod._stop_run(run)


async def test_cancel_during_the_encoder_test_stops_the_render(fake, tmp_path, monkeypatch):
    # Issue #790 (1): the encoder test before a render ran to its end (up to
    # about two minutes) before a cancel was seen.
    fake.probe = {"h264_nvenc": "hang"}
    started = time.monotonic()
    job = SimpleNamespace(id="abcdefabcdef")
    monkeypatch.setattr(takes_mod, "registry_get", lambda jid: job)
    run = takes_mod._RenderRun()
    run.gpu = takes_mod._GpuPlan(_build, 1920, 1080, 30)
    dest = tmp_path / "render.mp4"
    task = asyncio.create_task(
        takes_mod._render_task(run, job, _build(0.0, False, "libx264", 0), dest, 10.0)
    )
    await _cancel_after(run)
    try:
        await asyncio.wait_for(task, 5)
    finally:
        for proc in fake.procs:
            proc.kill()
    assert time.monotonic() - started < 5
    assert run.status == "none"
    probe = fake.procs[[fake.kind(c) for c in fake.calls].index("probe")]
    assert probe.killed, "the test encode is stopped"
    assert _renders(fake) == [], "nothing renders after a cancel"
    assert ve._cache == {}, "a stopped test says nothing about the card: not cached"


async def test_cancel_while_waiting_for_the_encoder_test_lock(fake, tmp_path, monkeypatch):
    # Issue #790 (1): Test encoders in Settings holds the probe lock for up
    # to about three minutes; a render waiting for it must still see a cancel.
    fake.probe = {"h264_nvenc": "ok"}
    job = SimpleNamespace(id="abcdefabcdef")
    monkeypatch.setattr(takes_mod, "registry_get", lambda jid: job)
    run = takes_mod._RenderRun()
    run.gpu = takes_mod._GpuPlan(_build, 1920, 1080, 30)
    ve._probe_lock.acquire()
    try:
        task = asyncio.create_task(
            takes_mod._render_task(
                run, job, _build(0.0, False, "libx264", 0), tmp_path / "render.mp4", 10.0
            )
        )
        await _cancel_after(run)
        done, _pending = await asyncio.wait({task}, timeout=5)
        assert done, "the cancel was seen while the lock was held"
    finally:
        ve._probe_lock.release()
    await asyncio.wait_for(task, 5)
    assert run.status == "none" and _renders(fake) == []
    assert fake.count("probe") == 0


def test_a_cancelled_render_never_starts_ffmpeg(fake, tmp_path):
    # Issue #790 (2): run_video_render started ffmpeg before its first check.
    with pytest.raises(take_render.RenderCancelled):
        take_render.run_video_render(
            _build(0.0, False, "libx264", 0),
            tmp_path / "render.mp4",
            10.0,
            cancelled=lambda: True,
            report=lambda share: None,
            on_proc=lambda proc: None,
            timeout=60,
        )
    assert _renders(fake) == [], "no ffmpeg process was started"
    assert not list(tmp_path.glob("*render.mp4*"))


async def test_master_retry_uses_the_encoder_that_held(fake, tmp_path, monkeypatch):
    """The master's over-ceiling retry renders again; after a GPU failure it
    stays on the CPU, with the retry's own (trim, alt) command."""
    fake.probe = {"h264_nvenc": "ok"}
    fake.render = {"h264_nvenc": "fail"}
    built = []

    def retry_build(trim, alt):
        built.append((trim, alt))
        return _build(trim, alt, "libx264", 0)

    measures = iter([SimpleNamespace(tp=0.5), SimpleNamespace(tp=-2.0)])

    async def measure(_path):
        return next(measures)

    monkeypatch.setattr(stems_mod, "measure_file", measure)
    retry = takes_mod._MasterRetry(retry_build, -1.0)
    run, _dest = await _render(tmp_path, monkeypatch, retry=retry)
    assert run.status == "done" and run.fallback == "exit"
    assert built == [(0.0, True)]
    assert _renders(fake) == ["h264_nvenc", "libx264", "libx264"]


# ─── Endpoints ───────────────────────────────────────────────────────────


@pytest.fixture
def client():
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_settings_expose_and_validate_the_encoder(client):
    async with client as c:
        assert (await c.get("/api/settings")).json()["video_encoder"] == "auto"
        r = await c.post("/api/settings", json={"video_encoder": "h264_nvenc"})
        assert r.status_code == 200 and r.json()["video_encoder"] == "h264_nvenc"
        for bad in ("h264_mf", "../../etc/passwd", "cpu && calc", 5, None, ["cpu"]):
            r = await c.post("/api/settings", json={"video_encoder": bad})
            assert r.status_code == 422, bad
            assert "passwd" not in r.text and "calc" not in r.text
        assert (await c.get("/api/settings")).json()["video_encoder"] == "h264_nvenc"
    saved = json.loads(settings_mod._SETTINGS_PATH.read_text(encoding="utf-8"))
    assert saved["video_encoder"] == "h264_nvenc"


async def test_get_video_encoders_reads_the_cache_without_probing(fake, client):
    fake.probe = {"h264_nvenc": "ok"}
    ve.probe("h264_nvenc", 1920, 1080, 30)
    async with client as c:
        r = await c.get("/api/video-encoders")
    assert r.status_code == 200
    body = r.json()
    assert body["setting"] == "auto" and body["effective"] == "auto"
    assert body["candidates"] == ["h264_nvenc", "h264_qsv"]
    assert body["ffmpeg"] == "ffmpeg version 9.9-fake" and body["tested"] is False
    assert body["results"][0]["encoder"] == "h264_nvenc"
    assert body["results"][0]["results"]["1080"]["ok"] is True
    assert fake.count("probe") == 1


async def test_post_test_probes_every_candidate_at_1080p_and_4k(fake, client):
    fake.probe = {"h264_nvenc": "ok", "h264_qsv": "fail"}
    async with client as c:
        r = await c.post("/api/video-encoders/test")
    assert r.status_code == 200
    rows = {row["encoder"]: row for row in r.json()["results"]}
    assert list(rows) == ["h264_nvenc", "h264_qsv", "libx264"]
    assert rows["h264_nvenc"]["results"]["2160"]["ok"] is True
    assert rows["h264_nvenc"]["family"] == "nvidia"
    assert rows["h264_qsv"]["results"]["1080"]["reason"] == "failed"
    assert rows["libx264"]["results"]["1080"]["ok"] is True
    sizes = {a for c in fake.calls for a in c if a.startswith("testsrc2=")}
    assert sizes == {"testsrc2=s=1920x1080:r=30:d=2", "testsrc2=s=3840x2160:r=30:d=2"}


async def test_post_test_takes_no_input_into_a_command(fake, client):
    fake.probe = {"h264_nvenc": "ok"}
    async with client as c:
        r = await c.post(
            "/api/video-encoders/test", json={"encoder": "h264_mf; calc", "size": "99999x1"}
        )
    assert r.status_code == 200
    joined = " ".join(" ".join(c) for c in fake.calls)
    assert "calc" not in joined and "99999" not in joined and "h264_mf" not in joined


async def test_post_test_has_a_time_limit(fake, client, monkeypatch):
    fake.probe = {"h264_nvenc": "ok"}
    monkeypatch.setattr(video_encoders_api, "VIDEO_ENCODER_TEST_BUDGET_SEC", 0)
    async with client as c:
        r = await c.post("/api/video-encoders/test")
    assert r.status_code == 200
    reasons = {res["reason"] for row in r.json()["results"] for res in row["results"].values()}
    assert reasons == {"budget"}
    assert fake.count("probe") == 0

    def slow(_budget):
        time.sleep(1.5)
        return []

    monkeypatch.setattr(ve, "test_all", slow)
    monkeypatch.setattr(video_encoders_api, "VIDEO_ENCODER_TEST_BUDGET_SEC", -59.8)
    from app.main import app

    again = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    async with again as c:
        r = await c.post("/api/video-encoders/test")
    assert r.status_code == 504


async def test_post_test_one_at_a_time_and_generic_errors(fake, client, monkeypatch):
    async with client as c:
        await video_encoders_api._test_lock.acquire()
        try:
            r = await c.post("/api/video-encoders/test")
            assert r.status_code == 409
        finally:
            video_encoders_api._test_lock.release()

        def broken(_budget):
            raise RuntimeError("C:\\secret\\path exploded")

        monkeypatch.setattr(ve, "test_all", broken)
        r = await c.post("/api/video-encoders/test")
        assert r.status_code == 500 and "secret" not in r.text
        assert r.json()["detail"] == "the encoder test failed"


# ─── Real ffmpeg: the fallback ends in a valid libx264 MP4 ───────────────


@pytest.mark.parametrize(
    ("gpu_args", "reason"),
    [
        # An encoder this ffmpeg does not have: the GPU render exits non-zero.
        (["-c:v", "stemdeck_no_such_encoder"], "exit"),
        # A file that encodes but at the wrong size: the check catches it.
        (["-c:v", "libx264", "-preset", "ultrafast", "-s", "320x240"], "invalid-size"),
    ],
)
async def test_real_ffmpeg_fallback_produces_a_valid_mp4(tmp_path, monkeypatch, gpu_args, reason):
    from tests.test_takes_render import (
        JOB,
        _make_job,
        _needs_x264,
        _probe,
        _take_with_camera,
        _wait,
    )

    _needs_x264()
    from app.core import config

    takes_mod._renders.clear()
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    _make_job(config.JOBS_DIR)
    monkeypatch.setattr(ve, "VIDEO_ENCODER_OVERRIDE", "")
    monkeypatch.setattr(ve, "choose", lambda w, h, f, *a: ve.Choice("h264_nvenc"))
    monkeypatch.setattr(ve, "encoder_args", lambda *a, **k: list(gpu_args))
    from app.main import app

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    async with client as c:
        tid = await _take_with_camera(c, tmp_path, first_frame_ms=0.0)
        body = {"canvas": "16:9", "fit": "fill", "size": "720", "fps": 30}
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/render", json=body)
        assert r.status_code == 202, r.text
        view = await _wait(c, tid)
    takes_mod._renders.clear()
    assert view["status"] == "done", view
    assert view["encoder"] == "libx264" and view["fallback"] is True
    assert view["fallback_reason"] == reason
    info = _probe(config.JOBS_DIR / JOB / "takes" / tid / "render.mp4")
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == "h264" and (video["width"], video["height"]) == (1280, 720)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert take_render.VIDEO_CPU_ENCODER == "libx264"
