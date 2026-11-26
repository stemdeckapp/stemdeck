"""The DaVinci Resolve package of a take (POST /api/jobs/{id}/takes/{take}/resolve).

The end-to-end tests run the real ffmpeg on a camera clip that flashes once
and a take that beeps once, unpack the .otioz and measure every file: the
frame count, the sample count, no B-frames, the start at 0, and where the
flash and the beep landed. The pure parts are in test_resolve_timeline.py.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from urllib.parse import unquote

import numpy as np
import pytest
import soundfile as sf

from app.api import takes as takes_mod
from tests.ffmpeg_probe import skip_without_ffmpeg
from tests.test_takes_render import (
    JOB,
    SONG_SEC,
    _flash_take,
    _frame_means,
    _make_job,
    _needs_x264,
    _probe,
    _take_with_camera,
    client,  # noqa: F401 -- fixture
    jobs_dir,  # noqa: F401 -- fixture
)

RATE = 48000
# A right-to-left override: it can make "gpj.exe" read as "exe.jpg".
RLO = chr(0x202E)


async def _wait_package(c, tid, timeout=180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = (await c.get(f"/api/jobs/{JOB}/takes/{tid}/resolve")).json()
        if view["status"] in ("done", "error", "none"):
            return view
        await asyncio.sleep(0.2)
    raise AssertionError("package did not finish")


async def _package(c, tid, **body) -> dict:
    r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/resolve", json=body)
    assert r.status_code == 202, r.text
    view = await _wait_package(c, tid)
    assert view["status"] == "done", view
    return view


async def _unpack(c, tid, out_dir, name=None) -> tuple[dict, list[str], str]:
    """The package downloaded and unpacked: (timeline, member names,
    Content-Disposition)."""
    url = f"/api/jobs/{JOB}/takes/{tid}/resolve.otioz"
    r = await c.get(url, params={"name": name} if name else None)
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
        zf.extractall(out_dir)
    timeline = json.loads((out_dir / "content.otio").read_text(encoding="utf-8"))
    return timeline, names, unquote(r.headers["content-disposition"])


def _wav_onset(path) -> int:
    data, rate = sf.read(path, dtype="float32")
    assert rate == RATE
    (at,) = _onsets_at(data[:, 0], rate)
    return at


def _onsets_at(x, rate, threshold=0.1):
    loud = np.flatnonzero(np.abs(x) > threshold)
    if not len(loud):
        return []
    keep = [int(loud[0])]
    keep += [int(b) for a, b in zip(loud[:-1], loud[1:], strict=True) if b - a > 0.3 * rate]
    return keep


# ─── The package, measured ───────────────────────────────────────────────


async def test_the_package_lines_up_every_file_to_the_frame(jobs_dir, client, tmp_path):  # noqa: F811
    """The whole song (3 s at 30 fps: 90 frames, 144000 samples). The flash
    (camera frame 30, song 0.995 s) is on frame 30 of the picture; the beep
    (song 1.8 s) is at 1.8 s in the take's WAV within 1 ms; every WAV is
    exactly 90 frames long, 48 kHz stereo 24-bit; the picture has no sound,
    no B-frames and starts at 0; the timeline names every file."""
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _flash_take(c, tmp_path, first_frame_ms=-5.0, beep_at=1.8)
        view = await _package(
            c,
            tid,
            span="song",
            size="720",
            stems=["vocals", "drums"],
            gains=[1.0, 0.5],
            name="Song - Artist (Performance)",
        )
        assert view["url"] == f"/api/jobs/{JOB}/takes/{tid}/resolve.otioz"
        assert view["large"] is False and view["estimate_bytes"] > 0
        out = tmp_path / "unpacked"
        timeline, names, disposition = await _unpack(
            c, tid, out, "Song - Artist (Performance).otioz"
        )
        # The MP4 render is another file, untouched.
        assert (await c.get(f"/api/jobs/{JOB}/takes/{tid}/render")).json()["status"] == "none"
    assert "Song - Artist (Performance).otioz" in disposition
    assert names == [
        "version.txt",
        "content.otio",
        "media/Song_video.mp4",
        f"media/Song_take_{tid}.wav",
        "media/Song_backing.wav",
    ]
    assert timeline["name"] == "Song - Artist (Performance)"
    assert timeline["global_start_time"]["value"] == 108000.0
    tracks = timeline["tracks"]["children"]
    assert [t["kind"] for t in tracks] == ["Video", "Audio", "Audio"]
    for track in tracks:
        (clip,) = track["children"]
        assert clip["source_range"]["start_time"]["value"] == 0.0
        assert clip["source_range"]["duration"] == {
            "OTIO_SCHEMA": "RationalTime.1",
            "rate": 30.0,
            "value": 90.0,
        }
        assert (out / clip["media_reference"]["target_url"]).is_file()

    video = out / "media" / "Song_video.mp4"
    info = _probe(video)
    assert [s["codec_type"] for s in info["streams"]] == ["video"]
    stream = info["streams"][0]
    assert int(stream["nb_frames"]) == 90
    assert int(stream["has_b_frames"]) == 0
    assert float(stream["start_time"]) == 0.0
    assert (int(stream["width"]), int(stream["height"])) == (1280, 720)
    assert stream["r_frame_rate"] == "30/1"
    means = _frame_means(video)
    assert len(means) == 90
    bright = np.flatnonzero(means > 128)
    assert list(bright) == [30], means

    for wav in ("Song_backing.wav", f"Song_take_{tid}.wav"):
        meta = sf.info(str(out / "media" / wav))
        assert (meta.frames, meta.samplerate, meta.channels) == (90 * 1600, RATE, 2)
        assert meta.subtype == "PCM_24"
    # The beep: song 1.8 s, the sine over the threshold on its third sample
    # at 44.1 kHz (test_takes_render's rule), within 1 ms at 48 kHz.
    at = _wav_onset(out / "media" / f"Song_take_{tid}.wav")
    assert abs(at - round(1.8 * RATE)) <= RATE // 1000, at - round(1.8 * RATE)
    # The backing: the two 0.2 sines at 1 and 0.5, summed, never clipped.
    backing, _ = sf.read(out / "media" / "Song_backing.wav", dtype="float32")
    assert 0.25 < float(np.abs(backing).max()) < 0.32


async def test_a_trimmed_package_starts_at_the_trim(jobs_dir, client, tmp_path):  # noqa: F811
    """A range of 1.0 s to 2.6 s at 24 fps: 38 frames (1.583 s), the beep
    (song 1.8 s) 0.8 s into the take's file, the flash (song 0.995 s, before
    the range) not in the picture."""
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _flash_take(c, tmp_path, first_frame_ms=-5.0, beep_at=1.8)
        await _package(c, tid, span="range", range_in=1.0, range_out=2.6, fps=24, size="720")
        out = tmp_path / "unpacked"
        timeline, _names, _disp = await _unpack(c, tid, out)
    frames = round(1.6 * 24)
    assert timeline["tracks"]["children"][0]["children"][0]["source_range"]["duration"] == {
        "OTIO_SCHEMA": "RationalTime.1",
        "rate": 24.0,
        "value": float(frames),
    }
    stream = _probe(out / "media" / "Song_video.mp4")["streams"][0]
    assert int(stream["nb_frames"]) == frames and float(stream["start_time"]) == 0.0
    assert (_frame_means(out / "media" / "Song_video.mp4") < 20).all()
    take = out / "media" / f"Song_take_{tid}.wav"
    assert sf.info(str(take)).frames == frames * 2000
    at = _wav_onset(take)
    assert abs(at - round(0.8 * RATE)) <= RATE // 1000, at - round(0.8 * RATE)


async def test_stems_put_each_lane_on_its_own_track(jobs_dir, client, tmp_path):  # noqa: F811
    """ "Song as separate stems": one WAV per lane as heard, a muted lane
    (gain 0) left out, each the full length."""
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        await _package(
            c,
            tid,
            span="song",
            size="720",
            backing="stems",
            stems=["vocals", "drums"],
            gains=[0.0, 1.0],
        )
        out = tmp_path / "unpacked"
        timeline, names, _disp = await _unpack(c, tid, out)
    assert names[2:] == [
        "media/Song_video.mp4",
        f"media/Song_take_{tid}.wav",
        "media/Song_drums.wav",
    ]
    assert [t["name"] for t in timeline["tracks"]["children"]] == ["Video", "Take 1", "Drums"]
    assert sf.info(str(out / "media" / "Song_drums.wav")).frames == round(SONG_SEC * 30) * 1600


# ─── Refusals and guards ─────────────────────────────────────────────────


async def test_package_refusals(jobs_dir, client, tmp_path, monkeypatch):  # noqa: F811
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        base = f"/api/jobs/{JOB}/takes/{tid}/resolve"
        monkeypatch.setattr(takes_mod, "libx264_available", lambda: True)
        for payload in (
            {"backing": "all"},
            {"master": "eyJ2IjoxfQ"},  # no master in a package
            {"fps": 25},
            {"canvas": "4:3"},
            {"name": "x" * 201},
            {"stems": ["nope"], "gains": [1]},
            {"stems": [tid], "gains": [1]},
            {"span": "range", "range_in": 2, "range_out": 1},
            {"unknown": 1},
            # No beat grid in this song: a click asked for is refused, never dropped.
            {"click": True},
        ):
            r = await c.post(base, json=payload)
            assert r.status_code == 422, (payload, r.text)
        r = await c.post(
            base, content=b'{"take_gain": NaN}', headers={"content-type": "application/json"}
        )
        assert r.status_code == 422
        assert (await c.post(base, json={"click": True})).json()[
            "detail"
        ] == "this song has no beat grid"
        # Unknown job, unknown take, a malformed id, traversal: 404, never 500.
        assert (
            await c.post(f"/api/jobs/000000000000/takes/{tid}/resolve", json={})
        ).status_code == 404
        assert (
            await c.post(f"/api/jobs/{JOB}/takes/t00000000/resolve", json={})
        ).status_code == 404
        assert (await c.get(f"/api/jobs/{JOB}/takes/T0000AAAA/resolve")).status_code == 404
        for evil in ("..%2F..%2Fstems", "%2E%2E"):
            r = await c.post(f"/api/jobs/{JOB}/takes/{evil}/resolve", json={})
            assert r.status_code in (404, 405), (evil, r.status_code)
        r = await c.get(
            f"/api/jobs/{JOB}/takes/{tid}/resolve.otioz", params={"name": "../../x.otioz"}
        )
        assert r.status_code == 404  # none made yet
        # No disk space: 507, and nothing is left registered.
        monkeypatch.setattr(
            takes_mod.shutil, "disk_usage", lambda _p: type("U", (), {"free": 1000})()
        )
        r = await c.post(base, json={"size": "720"})
        assert r.status_code == 507 and "disk space" in r.json()["detail"]
        assert (JOB, tid) not in takes_mod._renders
        monkeypatch.setattr(takes_mod, "libx264_available", lambda: False)
        r = await c.post(base, json={})
        assert r.status_code == 422 and "libx264" in r.json()["detail"]
        # No camera: the render's own 422.
        (jobs_dir / JOB / "takes" / tid / "camera.mp4").unlink()
        r = await c.post(base, json={})
        assert r.status_code == 422 and r.json()["detail"] == "take has no camera"


async def test_one_export_per_take_of_either_kind(jobs_dir, client, tmp_path):  # noqa: F811
    """One registry: a package running holds off the video render, an edit,
    a sync change and its own download; the video render's Cancel does not
    stop it; a delete does."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        base = f"/api/jobs/{JOB}/takes/{tid}"
        run = takes_mod._RenderRun(status="running", kind="resolve")
        takes_mod._renders[(JOB, tid)] = run
        r = await c.post(f"{base}/render", json={})
        assert r.status_code in (409, 422)  # 422 when this ffmpeg has no libx264
        if r.status_code == 409:
            assert "DaVinci Resolve" in r.json()["detail"]
        assert (await c.patch(base, json={"label": "x"})).status_code == 409
        assert (await c.post(f"{base}/sync", json={"nudgeMs": 10})).status_code == 409
        assert (await c.get(f"{base}/resolve.otioz")).status_code == 409
        assert (await c.get(f"{base}/render")).json()["status"] == "none"
        assert (await c.get(f"{base}/resolve")).json()["status"] == "running"
        await c.post(f"{base}/render/cancel")
        assert not run.cancel.is_set() and takes_mod._renders[(JOB, tid)] is run
        # And the other way round: a video render holds off a package.
        takes_mod._renders[(JOB, tid)] = takes_mod._RenderRun(status="running")
        r = await c.post(f"{base}/resolve", json={})
        assert r.status_code in (409, 422)
        if r.status_code == 409:
            assert r.json()["detail"] == "video render already running"
        await c.post(f"{base}/resolve/cancel")
        assert takes_mod._renders[(JOB, tid)].status == "running"
        takes_mod._renders[(JOB, tid)] = run
        r = await c.post(f"{base}/resolve/cancel")
        assert r.json()["status"] == "none" and run.cancel.is_set()
        takes_mod._renders[(JOB, tid)] = run2 = takes_mod._RenderRun(
            status="running", kind="resolve"
        )
        assert (await c.delete(base)).status_code == 200
        assert run2.cancel.is_set() and (JOB, tid) not in takes_mod._renders


async def test_a_moved_picture_or_sound_drops_the_package(jobs_dir, client, tmp_path):  # noqa: F811
    """A nudge or a sync change drops resolve.otioz with render.mp4: neither
    matches any more. A rename keeps both."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        base = f"/api/jobs/{JOB}/takes/{tid}"
        folder = jobs_dir / JOB / "takes" / tid
        package, video = folder / "resolve.otioz", folder / "render.mp4"

        def make():
            package.write_bytes(b"PK")
            video.write_bytes(b"x")

        make()
        assert (await c.patch(base, json={"label": "Renamed"})).status_code == 200
        assert package.exists() and video.exists()
        assert (await c.get(f"{base}/resolve")).json()["status"] == "done"
        assert (await c.patch(base, json={"user_nudge_ms": 20})).status_code == 200
        assert not package.exists() and not video.exists()
        make()
        assert (await c.post(f"{base}/sync", json={"nudgeMs": 40})).status_code == 200
        assert not package.exists() and not video.exists()
        assert (await c.get(f"{base}/resolve")).json()["status"] == "none"


async def test_cancel_leaves_no_temp_and_the_name_is_made_safe(jobs_dir, client, tmp_path):  # noqa: F811
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        base = f"/api/jobs/{JOB}/takes/{tid}"
        folder = jobs_dir / JOB / "takes" / tid
        r = await c.post(f"{base}/resolve", json={"span": "song"})
        assert r.status_code == 202
        await c.post(f"{base}/resolve/cancel")
        await asyncio.sleep(1.5)
        assert (await c.get(f"{base}/resolve")).json()["status"] in (
            "none",
            "done",  # a 3 s package on a fast machine can win the race
        )
        for _ in range(50):
            if not list(folder.glob(".resolve*")):
                break
            await asyncio.sleep(0.1)
        assert list(folder.glob(".resolve*")) == []

        await _package(c, tid, span="song", size="720")
        assert list(folder.glob(".resolve*")) == []
        url = f"{base}/resolve.otioz"
        r = await c.get(url, params={"name": f'../a{RLO}"b".otioz'})
        assert r.status_code == 200
        disposition = unquote(r.headers["content-disposition"])
        assert disposition.endswith("filename*=utf-8''a b .otioz"), disposition
        assert RLO not in disposition and "/" not in disposition.split("filename", 1)[1]
        # Not an .otioz name: the default one.
        r = await c.get(url, params={"name": "x.mp4"})
        assert "Song_Take_1.otioz" in r.headers["content-disposition"]


@pytest.mark.parametrize("backing", ["mix", "stems"])
def test_a_song_with_no_lanes_has_no_backing_file(backing):
    """Nothing to hear under the take: no backing file at all, rather than a
    silent one."""
    from app.core.models import Job

    audio = takes_mod._RenderAudio([], "", "", [])
    assert takes_mod._package_audio(Job(id=JOB, status="done"), audio, backing) == []
