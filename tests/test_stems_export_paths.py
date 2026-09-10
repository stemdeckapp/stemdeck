"""The export endpoints' validation, cleanup and video path.

What was left in stems.py after the click lane and the mp3 cache: the trim
validation both stem endpoints share, the zip builder's subset rules, the video
export's own refusals, and the cleanup that runs when a render is killed or a
temp file cannot be removed.

The cleanup half is the part that matters least per line and most in aggregate:
every one of these paths runs while something is already going wrong, and each
leaks a file or a process if it is missed.
"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
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

    job = Job(id=JOB)
    job.status = "done"
    job.title = "Get Lucky"
    job.duration_sec = 1.0
    _jobs[JOB] = job
    return tmp_path, stems, job


@pytest.fixture
def client(library):
    from app.main import app

    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------
# trim validation, shared by the wav and mp3 stem endpoints
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ext", ["wav", "mp3"])
@pytest.mark.parametrize(
    "params, why",
    [
        ({"start": 0.5}, "only a start"),
        ({"end": 0.5}, "only an end"),
        ({"start": 0.8, "end": 0.2}, "end before start"),
        ({"start": 0.5, "end": 0.5}, "a zero-length region"),
    ],
)
def test_a_half_specified_trim_is_refused(client, ext, params, why):
    """Both bounds or neither. A one-sided trim would silently export the whole
    stem, which is not what the region the user dragged asked for."""
    r = client.get(f"/api/jobs/{JOB}/stems/vocals.{ext}", params=params)

    assert r.status_code == 422, why
    assert "start and end" in r.json()["detail"]


@pytest.mark.parametrize("ext", ["wav", "mp3"])
def test_an_untrimmed_stem_is_served_whole(client, ext):
    r = client.get(f"/api/jobs/{JOB}/stems/vocals.{ext}")

    assert r.status_code == 200
    assert len(r.content) > 0


def test_a_trimmed_stem_is_shorter_than_the_whole_one(client, tmp_path):
    """The region trim is what the loop export is built on."""
    whole = client.get(f"/api/jobs/{JOB}/stems/vocals.wav")
    part = client.get(f"/api/jobs/{JOB}/stems/vocals.wav", params={"start": 0.0, "end": 0.25})

    assert part.status_code == 200
    assert 0 < len(part.content) < len(whole.content)


# --------------------------------------------------------------------------
# the stems zip
# --------------------------------------------------------------------------


def test_the_zip_contains_every_stem_by_default(client):
    r = client.get(f"/api/jobs/{JOB}/stems/all.zip")

    assert r.status_code == 200
    with zipfile.ZipFile(__import__("io").BytesIO(r.content)) as z:
        names = z.namelist()
    assert any("vocals" in n for n in names)
    assert any("drums" in n for n in names)


def test_a_zip_can_be_narrowed_to_a_subset(client):
    r = client.get(f"/api/jobs/{JOB}/stems/all.zip", params={"stems": "vocals"})

    assert r.status_code == 200
    with zipfile.ZipFile(__import__("io").BytesIO(r.content)) as z:
        names = z.namelist()
    assert any("vocals" in n for n in names)
    assert not any("drums" in n for n in names)


def test_a_zip_of_a_stem_that_does_not_exist_is_refused(client):
    r = client.get(f"/api/jobs/{JOB}/stems/all.zip", params={"stems": "kazoo"})

    assert r.status_code == 422
    assert "unknown stem" in r.json()["detail"]


def test_a_zip_cannot_mix_vocals_with_its_own_split(client):
    """lead_vocals + backing_vocals already contain everything vocals holds;
    including all three would put the same signal in the archive twice."""
    r = client.get(f"/api/jobs/{JOB}/stems/all.zip", params={"stems": "vocals,lead_vocals"})

    assert r.status_code == 422
    assert "same signal" in r.json()["detail"]


def test_a_job_whose_stems_folder_is_gone_has_no_zip(client, library):
    """Deleted or moved outside the app between the library listing and the
    download."""
    import shutil

    _root, stems, _job = library
    shutil.rmtree(stems)

    r = client.get(f"/api/jobs/{JOB}/stems/all.zip")

    assert r.status_code == 404


def test_a_zip_of_a_job_with_no_stem_files_is_not_found(client, library):
    _root, stems, _job = library
    for p in stems.glob("*.wav"):
        p.unlink()

    r = client.get(f"/api/jobs/{JOB}/stems/all.zip")

    assert r.status_code == 404


def test_a_zip_that_cannot_be_built_is_a_500_and_leaves_no_temp_file(client, monkeypatch, caplog):
    """The archive is built into a temp file; failing partway would otherwise
    leave one per attempt in the system temp dir."""
    import tempfile

    seen: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _tracking(*a, **kw):
        fd, name = real_mkstemp(*a, **kw)
        seen.append(Path(name))
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", _tracking)

    def _boom(*a, **kw):
        raise RuntimeError("zlib exploded")

    monkeypatch.setattr(stems_mod, "_build_stems_zip", _boom)

    r = client.get(f"/api/jobs/{JOB}/stems/all.zip")

    assert r.status_code == 500
    assert "failed to build archive" in r.json()["detail"]
    assert "failed to build stems zip" in caplog.text
    assert not any(p.exists() for p in seen), "a failed zip left its temp file behind"


# --------------------------------------------------------------------------
# the video export
# --------------------------------------------------------------------------


def test_a_video_export_for_a_job_with_no_video_is_not_found(client):
    r = client.get(f"/api/jobs/{JOB}/video.mp4", params={"stems": "vocals", "gains": "1"})

    assert r.status_code == 404
    assert "no video track" in r.json()["detail"]


def test_a_transposed_video_export_is_refused_without_rubberband(client, library, monkeypatch):
    """Same refusal as the audio export: a video whose audio came back in the
    original key is the same complaint reported again (#592)."""
    root, _stems, _job = library
    (root / JOB / "video.mp4").write_bytes(b"\0" * 64)
    monkeypatch.setattr(stems_mod, "_rubberband_available", lambda: False)

    r = client.get(
        f"/api/jobs/{JOB}/video.mp4",
        params={"stems": "vocals", "gains": "1", "pitches": "2"},
    )

    assert r.status_code == 422
    assert "cannot transpose" in r.json()["detail"]


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------


def test_a_rendered_export_that_cannot_be_deleted_is_only_logged(tmp_path, monkeypatch, caplog):
    """It runs after the response has been sent, so raising would surface as an
    error on a download that already succeeded."""
    import logging

    caplog.set_level(logging.DEBUG, logger="stemdeck.api")

    def _boom(self, **kw):
        raise OSError("in use")

    monkeypatch.setattr(Path, "unlink", _boom)

    stems_mod._unlink_later(tmp_path / "render.wav")  # must not raise

    assert "could not remove rendered export" in caplog.text


def test_a_rendered_export_is_deleted_once_it_has_been_sent(tmp_path):
    path = tmp_path / "render.wav"
    path.write_bytes(b"x")

    stems_mod._unlink_later(path)

    assert not path.exists()


async def test_a_stream_that_is_abandoned_kills_ffmpeg_and_drops_its_partial_cache(
    tmp_path, monkeypatch
):
    """A listener who seeks away mid-render leaves ffmpeg running and a partial
    file in the cache. Both have to go, or the next request serves the truncated
    render as if it were complete."""
    cache = tmp_path / "cache" / "mix.wav"
    cache.parent.mkdir(parents=True)

    class _Stdout:
        async def read(self, _n):
            return b"partial-audio"

    class _Stderr:
        async def readline(self):
            return b""

    class _Proc:
        returncode = None

        def __init__(self):
            self.killed = False

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    proc = _Proc()

    async def _exec(*a, **kw):
        proc.stdout = _Stdout()
        proc.stderr = _Stderr()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)

    agen = stems_mod._stream_ffmpeg(["ffmpeg"], cache_path=cache)
    assert await agen.__anext__() == b"partial-audio"
    await agen.aclose()  # the listener went away

    assert proc.killed, "ffmpeg was left running after the listener left"
    assert not cache.exists(), "a partial render was promoted into the cache"
    assert not list(cache.parent.glob(".*.tmp")), "the partial temp file was left behind"
