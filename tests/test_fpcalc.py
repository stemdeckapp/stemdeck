"""fpcalc, the fingerprint for an FFmpeg without the chromaprint muxer.

Subprocess is stubbed throughout, except the one test that runs fpcalc for
real, which skips on a machine that has none (STEMDECK_FPCALC points it at one).
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

import app.pipeline.fpcalc as fp
import app.pipeline.identify as ident
from tests.ffmpeg_probe import ffmpeg_available

FINGERPRINT = "AQAAjEmUaEkSZSoAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
FPCALC_JSON = ('{"duration": 20.00, "fingerprint": "' + FINGERPRINT + '"}\n').encode()
NO_MUXER = b"[out#0] Requested output format 'chromaprint' is not known."


class FakeProc:
    """A Popen that answers at once when given a returncode, else runs until
    killed. Enough of one to stand upstream of fpcalc too."""

    def __init__(self, cmd, answer, kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.killed = threading.Event()
        self.returncode = answer.get("returncode")
        self.stdout_data = answer.get("stdout", b"")
        self.stderr_data = answer.get("stderr", b"")
        self.stdout = _Pipe() if kwargs.get("stdout") is subprocess.PIPE else None
        self.waited = False

    def communicate(self, timeout=None):
        if self.returncode is not None:
            return self.stdout_data, self.stderr_data
        if self.killed.wait(timeout if timeout is not None else 5):
            self.returncode = -9
            return b"", b""
        raise subprocess.TimeoutExpired(self.cmd, timeout)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            self.returncode = -9
        return self.returncode

    def kill(self):
        self.killed.set()


class _Pipe:
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def spawn(monkeypatch):
    """Popen stubbed by what it runs: ``answers[name]`` gives the FakeProc
    kwargs for a command whose executable ends in ``name``. Every spawn is
    kept, in order."""
    spawned: list[FakeProc] = []
    answers: dict[str, dict] = {}

    def popen(cmd, **kwargs):
        name = next(n for n in answers if Path(cmd[0]).name.startswith(n))
        if isinstance(answers[name], Exception):
            raise answers[name]
        proc = FakeProc(cmd, answers[name], kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(ident, "_NO_CHROMAPRINT", set())
    monkeypatch.setattr(ident.subprocess, "Popen", popen)
    monkeypatch.setattr(ident, "ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(fp, "fpcalc_executable", lambda: "/opt/fpcalc")
    return spawned, answers


def _source(tmp_path: Path, name: str = "source.wav") -> Path:
    path = tmp_path / name
    path.write_bytes(b"RIFF")
    return path


# ── the helpers ──


def test_the_command_asks_for_json_and_two_minutes():
    assert fp.fpcalc_command("fpcalc", "/jobs/a/source.wav", 120) == [
        "fpcalc",
        "-json",
        "-length",
        "120",
        "/jobs/a/source.wav",
    ]


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (FPCALC_JSON, FINGERPRINT),
        (FPCALC_JSON.decode(), FINGERPRINT),
        (b'{"duration": 0.00, "fingerprint": ""}', ""),
        (b'{"duration": 20.0}', None),
        (b'{"fingerprint": 12}', None),
        (b'["AQAA"]', None),
        (b"ERROR: Could not open the input file", None),
        (b"", None),
        (None, None),
    ],
)
def test_parse_fpcalc_output(stdout, expected):
    assert fp.parse_fpcalc_output(stdout) == expected


def test_an_empty_fingerprint_is_not_one():
    """fpcalc prints an empty fingerprint for silence; AcoustID must not be
    asked about it."""
    assert ident.parse_fingerprint(fp.parse_fpcalc_output(b'{"fingerprint": ""}')) is None


def test_fpcalc_is_found_in_the_ffmpeg_folder_first(tmp_path, monkeypatch):
    own = tmp_path / "data" / fp.FPCALC_NAME
    beside = tmp_path / "bin" / fp.FPCALC_NAME
    ffmpeg = tmp_path / "bin" / "ffmpeg"
    for path in (own, beside, ffmpeg):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    monkeypatch.setattr(fp, "FPCALC_BIN", own)
    monkeypatch.setattr(fp, "ffmpeg_executable", lambda: str(ffmpeg))
    monkeypatch.setattr(fp.shutil, "which", lambda name: "/usr/bin/fpcalc")
    assert fp.fpcalc_executable() == str(own)
    own.unlink()
    assert fp.fpcalc_executable() == str(beside.resolve())
    beside.unlink()
    assert fp.fpcalc_executable() == "/usr/bin/fpcalc"
    monkeypatch.setattr(fp.shutil, "which", lambda name: None)
    assert fp.fpcalc_executable() is None


def test_a_bare_ffmpeg_on_path_is_not_looked_beside(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "FPCALC_BIN", tmp_path / "missing" / fp.FPCALC_NAME)
    monkeypatch.setattr(fp, "ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(fp.shutil, "which", lambda name: None)
    assert fp.fpcalc_executable() is None


# ── the fallback ──


def test_ffmpeg_without_the_muxer_falls_back_to_fpcalc(tmp_path, spawn):
    spawned, answers = spawn
    answers["ffmpeg"] = {"returncode": 1, "stderr": NO_MUXER}
    answers["fpcalc"] = {"returncode": 0, "stdout": FPCALC_JSON}
    source = _source(tmp_path)
    assert ident.fingerprint([source]) == FINGERPRINT
    assert [Path(p.cmd[0]).name for p in spawned] == ["ffmpeg", "fpcalc"]
    assert spawned[1].cmd == ["/opt/fpcalc", "-json", "-length", "120", str(source.absolute())]
    assert spawned[1].kwargs["stdin"] is subprocess.DEVNULL
    # Remembered: the next track goes straight to fpcalc.
    assert ident.fingerprint([source]) == FINGERPRINT
    assert [Path(p.cmd[0]).name for p in spawned] == ["ffmpeg", "fpcalc", "fpcalc"]


def test_ffmpeg_with_the_muxer_never_runs_fpcalc(tmp_path, spawn):
    spawned, answers = spawn
    answers["ffmpeg"] = {"returncode": 0, "stdout": FINGERPRINT.encode() + b"\n"}
    answers["fpcalc"] = {"returncode": 0, "stdout": FPCALC_JSON}
    assert ident.fingerprint([_source(tmp_path)]) == FINGERPRINT
    assert [Path(p.cmd[0]).name for p in spawned] == ["ffmpeg"]


def test_any_other_ffmpeg_failure_does_not_reach_for_fpcalc(tmp_path, spawn):
    spawned, answers = spawn
    answers["ffmpeg"] = {"returncode": 1, "stderr": b"source.wav: Invalid data found"}
    answers["fpcalc"] = {"returncode": 0, "stdout": FPCALC_JSON}
    assert ident.fingerprint([_source(tmp_path)]) is None
    assert [Path(p.cmd[0]).name for p in spawned] == ["ffmpeg"]
    assert not ident._NO_CHROMAPRINT


def test_no_fpcalc_either_leaves_the_tags(tmp_path, spawn, monkeypatch):
    spawned, answers = spawn
    answers["ffmpeg"] = {"returncode": 1, "stderr": NO_MUXER}
    monkeypatch.setattr(fp, "fpcalc_executable", lambda: None)
    source = _source(tmp_path)
    assert ident.fingerprint([source]) is None
    assert ident.fingerprint([source]) is None
    assert len(spawned) == 1, "FFmpeg asked once, and nothing else run"


def test_an_fpcalc_that_will_not_start_is_no_fingerprint(tmp_path, spawn):
    spawned, answers = spawn
    ident._NO_CHROMAPRINT.add("ffmpeg")
    answers["fpcalc"] = FileNotFoundError(2, "No such file", "/opt/fpcalc")
    assert ident.fingerprint([_source(tmp_path)]) is None


def test_a_failing_fpcalc_is_no_fingerprint(tmp_path, spawn):
    spawned, answers = spawn
    ident._NO_CHROMAPRINT.add("ffmpeg")
    answers["fpcalc"] = {"returncode": 2, "stderr": b"ERROR: Could not open the input file"}
    assert ident.fingerprint([_source(tmp_path)]) is None
    assert len(spawned) == 1


def test_a_slow_fpcalc_is_abandoned(tmp_path, spawn, monkeypatch):
    spawned, answers = spawn
    ident._NO_CHROMAPRINT.add("ffmpeg")
    answers["fpcalc"] = {}
    monkeypatch.setattr(ident, "TIMEOUT_FINGERPRINT", 0.3)
    started = time.monotonic()
    assert ident.fingerprint([_source(tmp_path)], job_id="abcdef000001") is None
    assert time.monotonic() - started < 3
    assert spawned[0].killed.is_set()
    assert "abcdef000001" not in ident._RUNNING


def test_release_source_stops_fpcalc(tmp_path, spawn):
    spawned, answers = spawn
    ident._NO_CHROMAPRINT.add("ffmpeg")
    answers["fpcalc"] = {}
    answer = {}
    thread = threading.Thread(
        target=lambda: answer.setdefault(
            "fp", ident.fingerprint([_source(tmp_path)], job_id="abcdef000002")
        )
    )
    thread.start()
    time.sleep(0.1)
    ident.release_source("abcdef000002")
    thread.join(5)
    assert spawned[0].killed.is_set()
    assert answer["fp"] is None


def test_stems_are_summed_by_ffmpeg_and_piped_to_fpcalc(tmp_path, spawn):
    spawned, answers = spawn
    ident._NO_CHROMAPRINT.add("ffmpeg")
    answers["ffmpeg"] = {}
    answers["fpcalc"] = {"returncode": 0, "stdout": FPCALC_JSON}
    stems = [_source(tmp_path, f"{n}.wav") for n in ("vocals", "drums")]
    assert ident.fingerprint(stems) == FINGERPRINT
    mix, fpcalc = spawned
    assert mix.cmd == ident.decode_command("ffmpeg", stems, 120) + ["-f", "wav", "-"]
    assert "chromaprint" not in mix.cmd
    assert fpcalc.cmd[-1] == "-"
    assert fpcalc.kwargs["stdin"] is mix.stdout
    assert mix.stdout.closed, "the parent's copy of the pipe is closed"
    assert mix.killed.is_set() and mix.waited, "FFmpeg is reaped with fpcalc"


def test_a_cancelled_stem_fingerprint_stops_both(tmp_path, spawn):
    spawned, answers = spawn
    ident._NO_CHROMAPRINT.add("ffmpeg")
    answers["ffmpeg"] = {}
    answers["fpcalc"] = {}
    stems = [_source(tmp_path, f"{n}.wav") for n in ("vocals", "drums")]
    started = time.monotonic()
    assert ident.fingerprint(stems, cancelled=lambda: time.monotonic() > started + 0.3) is None
    assert all(p.killed.is_set() for p in spawned)


def test_the_chromaprint_command_is_unchanged_by_the_split():
    stems = [Path("vocals.wav"), Path("drums.wav")]
    cmd = ident.fingerprint_command("ffmpeg", stems, 120)
    assert cmd[: len(cmd) - 7] == ident.decode_command("ffmpeg", stems, 120)
    assert cmd[-7:] == ["-f", "chromaprint", "-algorithm", "1", "-fp_format", "base64", "-"]


# ── for real ──


def _tone(path: Path, seconds: int, freq: int) -> None:
    from app.core.config import ffmpeg_executable

    subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=d={seconds}:a=0.05:seed=7",
            "-filter_complex",
            "amix=inputs=2",
            "-ac",
            "2",
            "-y",
            str(path),
        ],
        check=True,
        timeout=60,
    )


def test_a_real_fingerprint_from_fpcalc(tmp_path, monkeypatch):
    """The same fingerprint the muxer makes, when this FFmpeg has one to
    compare with; otherwise at least AcoustID's format."""
    exe = fp.fpcalc_executable()
    if exe is None or not ffmpeg_available():
        pytest.skip("no fpcalc (set STEMDECK_FPCALC) or no FFmpeg to make the audio")
    one, two = tmp_path / "vocals.wav", tmp_path / "drums.wav"
    _tone(one, 8, 440)
    _tone(two, 8, 220)
    monkeypatch.setattr(ident, "_NO_CHROMAPRINT", set())
    by_muxer = ident.fingerprint([one]), ident.fingerprint([one, two])
    has_muxer = not ident._NO_CHROMAPRINT
    ident._NO_CHROMAPRINT.add(ident.ffmpeg_executable())
    by_fpcalc = ident.fingerprint([one]), ident.fingerprint([one, two])
    for print_ in by_fpcalc:
        assert print_ and print_.startswith("AQ")
    if has_muxer:
        assert by_fpcalc == by_muxer
