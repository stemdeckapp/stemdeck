"""What the two ffmpeg helpers do when the render does not end cleanly.

Every branch here runs while something has already gone wrong: ffmpeg wedged,
its stderr drain not finishing, a process that cannot be reaped. None of them
change the response the client gets -- the status is either already committed
or already decided -- so the only thing they protect is the server: a process
left running, a pipe left undrained, a temp file left in the cache directory.

The 5 s and TIMEOUT_FFMPEG waits are what make these expensive to reach, so the
module's view of `asyncio` is replaced with one whose wait_for expires on the
call under test and behaves normally everywhere else.
"""

from __future__ import annotations

import asyncio
import zipfile
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api.stems as stems_mod
from app.core.models import Job
from app.core.registry import _jobs

JOB = "abcdefabcdef"


@pytest.fixture(autouse=True)
def _isolate():
    _jobs.clear()
    yield
    _jobs.clear()


class _ImpatientAsyncio:
    """The asyncio module, except that the chosen wait_for calls expire at once.

    Counted rather than matched on the awaitable: the two helpers each make
    their waits in a fixed order, and the ordinal is what names the branch.
    """

    def __init__(self, *expire_on: int):
        self._n = 0
        self._expire_on = set(expire_on)

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def wait_for(self, awaitable, timeout):
        self._n += 1
        if self._n in self._expire_on:
            # Close the coroutine we are abandoning; a task keeps running and
            # is the caller's to cancel, which is the branch under test.
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise TimeoutError
        return await asyncio.wait_for(awaitable, timeout)


@pytest.fixture
def hanging_drain(monkeypatch):
    """A stderr drain that never finishes, and a record of its cancellation."""
    state = {"cancelled": False}

    async def _never_finishes(stream, sink):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    monkeypatch.setattr(stems_mod, "_drain_stderr", _never_finishes)
    return state


class _Proc:
    """An ffmpeg that behaves however the test needs it to."""

    def __init__(self, *, chunks=(b"audio",), exits_with=0, reaps=True):
        self._chunks = [*chunks, b""]
        self._exits_with = exits_with
        self._reaps = reaps
        self.returncode = None
        self.killed = False
        outer = self

        class _Stdout:
            async def read(self, _n):
                # A real pipe read suspends; without a suspension here the
                # stderr drain task never gets a turn to start at all.
                await asyncio.sleep(0)
                return outer._chunks.pop(0)

        class _Stderr:
            async def readline(self):
                return b""

        self.stdout = _Stdout()
        self.stderr = _Stderr()

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        await asyncio.sleep(0)
        # A process that cannot be reaped leaves returncode None, which is what
        # the finally block's last-resort kill exists for.
        if self._reaps and self.returncode is None:
            self.returncode = self._exits_with
        return self.returncode


async def _settle():
    """Give the cancelled drain task the loop turns it needs to unwind."""
    for _ in range(5):
        await asyncio.sleep(0)


def _spawning(proc, monkeypatch):
    async def _exec(*a, **kw):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)


# --------------------------------------------------------------------------
# _stream_ffmpeg
# --------------------------------------------------------------------------


async def test_a_stderr_drain_that_will_not_finish_is_cancelled_not_awaited(
    monkeypatch, hanging_drain
):
    """The drain reads until EOF, and a killed ffmpeg whose stderr is inherited
    by a surviving child never delivers one. Awaiting it without a bound would
    hold the request handler open for as long as that child lives."""
    proc = _Proc()
    _spawning(proc, monkeypatch)
    monkeypatch.setattr(stems_mod, "asyncio", _ImpatientAsyncio(1))

    chunks = [chunk async for chunk in stems_mod._stream_ffmpeg(["ffmpeg"], context="mixdown")]
    await _settle()

    assert chunks == [b"audio"], "the client still got the whole render"
    assert hanging_drain["cancelled"], "the drain task was left running"


# --------------------------------------------------------------------------
# _render_to_file
# --------------------------------------------------------------------------


async def test_a_render_that_never_exits_is_killed_and_reported_as_a_failure(tmp_path, monkeypatch):
    """TIMEOUT_FFMPEG is the only bound on a wedged encoder. Unlike the streamed
    path the status is still ours to choose here, so the client gets an honest
    500 rather than a truncated file."""
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache")
    proc = _Proc()
    _spawning(proc, monkeypatch)
    monkeypatch.setattr(stems_mod, "asyncio", _ImpatientAsyncio(1))

    with pytest.raises(HTTPException) as excinfo:
        await stems_mod._render_to_file(["ffmpeg"], ".wav", context="export")

    assert excinfo.value.status_code == 500
    assert proc.killed, "a wedged ffmpeg was left running"
    assert not list((tmp_path / "cache").glob("*")), "the partial render was left behind"


async def test_a_file_render_whose_drain_hangs_still_completes(
    tmp_path, monkeypatch, hanging_drain
):
    """The same drain bound as the streamed path, on the side where the render
    itself succeeded: a stuck stderr reader must not fail an export whose
    output file is already correct."""
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache")
    proc = _Proc()
    _spawning(proc, monkeypatch)
    # The drain is the second wait_for the file renderer makes.
    monkeypatch.setattr(stems_mod, "asyncio", _ImpatientAsyncio(2))

    out = await stems_mod._render_to_file(["ffmpeg"], ".wav", context="export")
    await _settle()

    assert out.suffix == ".wav"
    assert hanging_drain["cancelled"]


async def test_a_process_that_could_not_be_reaped_is_killed_on_the_way_out(tmp_path, monkeypatch):
    """wait() returning without an exit code means the child is still there --
    a grandchild holding the process group, most often. Leaving it would hold
    an ffmpeg per failed export for the life of the server."""
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache")
    proc = _Proc(reaps=False)
    _spawning(proc, monkeypatch)

    with pytest.raises(HTTPException):
        await stems_mod._render_to_file(["ffmpeg"], ".wav", context="export")

    assert proc.killed
    assert not list((tmp_path / "cache").glob("*"))


# --------------------------------------------------------------------------
# the video export's lane assembly
# --------------------------------------------------------------------------


def _tone(path: Path, seconds=1.0, rate=8000, freq=440.0):
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    sf.write(str(path), (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32), rate)


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", tmp_path / "click-cache")
    stems = tmp_path / JOB / "stems"
    stems.mkdir(parents=True)
    for name in ("vocals", "drums"):
        _tone(stems / f"{name}.wav")
    (tmp_path / JOB / "video.mp4").write_bytes(b"\0" * 64)

    job = Job(id=JOB)
    job.status = "done"
    job.title = "Get Lucky"
    job.duration_sec = 1.0
    job.has_video = True
    _jobs[JOB] = job
    return tmp_path, stems, job


@pytest.fixture
def captured_video_cmd(monkeypatch):
    """Stand in for the video render so the filter graph is what is asserted."""
    seen: list[list[str]] = []

    async def _fake_stream(cmd, context="", cache_path=None):
        seen.append(list(cmd))
        yield b"mp4"

    monkeypatch.setattr(stems_mod, "_stream_ffmpeg", _fake_stream)
    return seen


@pytest.fixture
def client(library):
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_a_single_lane_video_export_skips_the_mixer(client, captured_video_cmd):
    """A one-input amix is a no-op that still costs a filter pass, and its
    output label differs -- mapping [mix] when no mixer was built would make
    ffmpeg fail on an otherwise valid export."""
    r = client.get(f"/api/jobs/{JOB}/video.mp4", params={"stems": "vocals", "gains": "1"})

    assert r.status_code == 200
    graph = captured_video_cmd[0][captured_video_cmd[0].index("-filter_complex") + 1]
    assert "amix" not in graph
    assert captured_video_cmd[0][captured_video_cmd[0].index("-map") + 1] == "[a0]"


def test_the_click_track_is_appended_before_the_video_input(
    client, captured_video_cmd, monkeypatch, tmp_path
):
    """Audio input indices have to stay contiguous from zero: the filter graph
    names them by position, and putting the video in between would point every
    lane after it at the wrong stream."""
    click = tmp_path / "click.wav"
    _tone(click, freq=1000.0)
    monkeypatch.setattr(stems_mod, "_click_lane", lambda *a, **kw: (click, 0.8))

    r = client.get(
        f"/api/jobs/{JOB}/video.mp4",
        params={"stems": "vocals", "gains": "1", "click": "true"},
    )

    assert r.status_code == 200
    cmd = captured_video_cmd[0]
    inputs = [cmd[i + 1] for i, token in enumerate(cmd) if token == "-i"]
    assert inputs[-1].endswith("video.mp4"), "the video is not the last input"
    assert str(click) in inputs[:-1], "the click was not appended to the audio lanes"
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2" in graph


# --------------------------------------------------------------------------
# the zip's per-stem transcode
# --------------------------------------------------------------------------


def test_a_stem_ffmpeg_cannot_transcode_fails_the_whole_archive(client, library, caplog):
    """Half an archive is worse than none: the user would unpack it, find the
    stem they wanted missing, and have no way to tell that from a separation
    that never produced it."""
    _root, stems, _job = library
    (stems / "drums.wav").write_bytes(b"not audio at all")

    r = client.get(f"/api/jobs/{JOB}/stems/all.zip", params={"format": "mp3"})

    assert r.status_code == 500
    assert "failed to build archive" in r.json()["detail"]
    assert "ffmpeg failed for drums" in caplog.text


def test_an_archive_of_stems_ffmpeg_can_read_is_built(client):
    """So the refusal above is about the broken stem and not about mp3."""
    r = client.get(f"/api/jobs/{JOB}/stems/all.zip", params={"format": "mp3"})

    assert r.status_code == 200
    with zipfile.ZipFile(BytesIO(r.content)) as z:
        assert all(name.endswith(".mp3") for name in z.namelist())
