"""The DaVinci Resolve package's pure parts: the OpenTimelineIO timeline
(app/pipeline/resolve_timeline.py), the ffmpeg commands and the .otioz
writer (app/pipeline/resolve_package.py), and the startup sweep of its
leftovers. The API and the sync measured in real files are in
test_takes_resolve.py."""

from __future__ import annotations

import json
import zipfile

import pytest

from app.pipeline import take_render
from app.pipeline.resolve_package import (
    estimate_bytes,
    package_audio_cmd,
    package_video_cmd,
    samples_per_frame,
    span_frames,
    span_start,
    write_otioz,
)
from app.pipeline.resolve_timeline import TimelineClip, build_otio, check_clips
from app.pipeline.take_render import AudioLane, RenderCancelled

CLIPS = [
    TimelineClip("video", "Video", "media/Song_video.mp4"),
    TimelineClip("audio", "Take 1", "media/Song_take_t00000001.wav"),
    TimelineClip("audio", "Backing", "media/Song_backing.wav"),
    TimelineClip("audio", "Click", "media/Song_click.wav"),
]


def _rt(value, rate=30.0):
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": value}


def _range(n, rate=30.0):
    return {"OTIO_SCHEMA": "TimeRange.1", "duration": _rt(n, rate), "start_time": _rt(0.0, rate)}


# ─── Timeline ────────────────────────────────────────────────────────────


def test_the_timeline_is_the_agreed_otio():
    """Golden: the whole content.otio of a four-track package, 90 frames at
    30 fps. Every clip from frame 0 to 90, every target relative to the
    bundle, the timeline at 01:00:00:00."""
    doc = json.loads(build_otio("Song - Artist (Performance)", 30, 90, CLIPS))

    def track(kind, name, target):
        return {
            "OTIO_SCHEMA": "Track.1",
            "metadata": {},
            "name": name,
            "source_range": None,
            "effects": [],
            "markers": [],
            "enabled": True,
            "children": [
                {
                    "OTIO_SCHEMA": "Clip.1",
                    "metadata": {},
                    "name": name,
                    "source_range": _range(90.0),
                    "effects": [],
                    "markers": [],
                    "enabled": True,
                    "media_reference": {
                        "OTIO_SCHEMA": "ExternalReference.1",
                        "metadata": {},
                        "name": target.split("/")[-1],
                        "available_range": _range(90.0),
                        "target_url": target,
                    },
                }
            ],
            "kind": kind,
        }

    assert doc == {
        "OTIO_SCHEMA": "Timeline.1",
        "metadata": {},
        "name": "Song - Artist (Performance)",
        "global_start_time": _rt(108000.0),
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "metadata": {},
            "name": "tracks",
            "source_range": None,
            "effects": [],
            "markers": [],
            "enabled": True,
            "children": [
                track("Video", "Video", "media/Song_video.mp4"),
                track("Audio", "Take 1", "media/Song_take_t00000001.wav"),
                track("Audio", "Backing", "media/Song_backing.wav"),
                track("Audio", "Click", "media/Song_click.wav"),
            ],
        },
    }


@pytest.mark.parametrize(("fps", "start"), [(24, 86400.0), (30, 108000.0), (60, 216000.0)])
def test_the_timeline_starts_at_one_hour_at_every_rate(fps, start):
    doc = json.loads(build_otio("x", fps, 10, CLIPS[:2]))
    assert doc["global_start_time"] == _rt(start, float(fps))
    clip = doc["tracks"]["children"][1]["children"][0]
    assert clip["source_range"] == _range(10.0, float(fps))


def test_a_name_with_any_character_stays_valid_json():
    name = 'Ça "va" \\ é中文'
    assert json.loads(build_otio(name, 30, 1, CLIPS[:1]))["name"] == name


@pytest.mark.parametrize(
    "clips",
    [
        [],
        [CLIPS[1]],  # no video
        [CLIPS[1], CLIPS[0]],  # video not first
        [CLIPS[0], TimelineClip("video", "V2", "media/b.mp4")],  # two videos
        [CLIPS[0], TimelineClip("audio", "A", "media/../a.wav")],
        [CLIPS[0], TimelineClip("audio", "A", "a.wav")],
        [CLIPS[0], TimelineClip("audio", "A", "media/sub/a.wav")],
        [CLIPS[0], TimelineClip("audio", "A", "media/a b.wav")],
        [CLIPS[0], TimelineClip("audio", "A", "/media/a.wav")],
        [CLIPS[0], TimelineClip("audio", "A", "file:///media/a.wav")],
        [CLIPS[0], TimelineClip("audio", "A", "media/a.mp3")],
        [CLIPS[0], TimelineClip("audio", "A", "media/a.mp4")],  # audio as a video file
        [TimelineClip("video", "V", "media/a.wav")],  # video as a WAV
        [CLIPS[0], TimelineClip("midi", "M", "media/a.wav")],
        # Two names that differ only in case overwrite each other on a
        # case-insensitive disk.
        [CLIPS[0], CLIPS[1], TimelineClip("audio", "B", "media/SONG_TAKE_T00000001.wav")],
    ],
)
def test_a_timeline_the_bundle_cannot_hold_is_refused(clips):
    with pytest.raises(ValueError):
        check_clips(clips)
    with pytest.raises(ValueError):
        build_otio("x", 30, 10, clips)


@pytest.mark.parametrize(("fps", "frames"), [(25, 10), (29.97, 10), (30, 0), (30, -1), (30, 1.5)])
def test_a_rate_or_length_outside_the_package_is_refused(fps, frames):
    with pytest.raises(ValueError):
        build_otio("x", fps, frames, CLIPS)


# ─── Frames, samples, span ───────────────────────────────────────────────


def test_every_rate_has_a_whole_number_of_samples_per_frame():
    assert [samples_per_frame(f) for f in (24, 30, 60)] == [2000, 1600, 800]
    for bad in (25, 50, 23):
        with pytest.raises(ValueError):
            samples_per_frame(bad)


def test_the_span_is_whole_frames_from_a_whole_sample():
    assert span_frames(3.0, 30) == 90
    assert span_frames(3.016, 30) == 90 and span_frames(3.017, 30) == 91
    assert span_frames(0.0, 24) == 1
    # The MP4 render's cut: the nearest 44.1 kHz sample.
    assert span_start(1.0) == 1.0
    assert span_start(0.5000001) == round(0.5000001 * 44100) / 44100
    assert span_start(-0.2) == 0.0


def test_the_estimate_grows_with_picture_and_files():
    one = estimate_bytes(1920, 1080, 30, 60.0, 2)
    assert one > 60 * 48000 * 3 * 2 * 2
    assert estimate_bytes(1920, 1080, 30, 60.0, 4) > one
    assert estimate_bytes(1920, 1080, 60, 60.0, 2) > one


# ─── Commands ────────────────────────────────────────────────────────────


def test_the_picture_command_is_the_agreed_encode(tmp_path):
    """Golden on the video arguments: CFR at the take's rate, exactly N
    frames, no sound, no B-frames (no edit list), a keyframe every second,
    CRF 18, faststart, no metadata; the MP4 render's canvas and placement."""
    cmd = package_video_cmd(tmp_path / "camera.webm", -0.25, 90, 30, "9:16", "720", "fill", False)
    i = cmd.index("-map")
    assert cmd[i:] == [
        "-map",
        "[vout]",
        "-an",
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-bf",
        "0",
        "-g",
        "30",
        "-r",
        "30",
        "-frames:v",
        "90",
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
    ]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.startswith("color=c=black:s=720x1280:r=30:d=3.033333[bg];")
    assert "[0:v]setpts=PTS-STARTPTS+(-0.250000)/TB,trim=start=0,scale=720:1280" in graph
    assert graph.endswith("overlay=eof_action=pass:shortest=0,fps=30,format=yuv420p[vout]")
    assert "-c:a" not in cmd and "aac" not in cmd
    with pytest.raises(ValueError):
        package_video_cmd(tmp_path / "c.webm", 0.0, 90, 25, "16:9", "1080", "fill", False)


def test_the_audio_command_cuts_mixes_resamples_and_ends_exactly(tmp_path):
    lanes = [
        AudioLane(tmp_path / "drums.wav", 0.5, ""),
        AudioLane(tmp_path / "bass.wav", 1.0, "rubberband=pitch=1.059463:transients=crisp,", "eq,"),
    ]
    cmd = package_audio_cmd(lanes, 1.5, 48000)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:a]atrim=start=1.500000,asetpts=PTS-STARTPTS,volume=0.500000[a0]" in graph
    assert "[1:a]atrim=start=1.500000,asetpts=PTS-STARTPTS,eq,rubberband=" in graph
    assert (
        f"[a0][a1]amix=inputs=2:normalize=0,aresample=48000,{take_render.EXPORT_LIMITER},"
        "apad=whole_len=48000,atrim=end_sample=48000[aout]"
    ) in graph
    joined = " ".join(cmd)
    for part in ("-ac 2", "-ar 48000", "-c:a pcm_s24le", "-map_metadata -1", "-fflags +bitexact"):
        assert part in joined
    # One lane, from song 0: no cut, no mix.
    single = package_audio_cmd(lanes[:1], 0.0, 1600)
    graph = single[single.index("-filter_complex") + 1]
    assert graph.startswith("[0:a]volume=0.500000[a0];[a0]aresample=48000,")
    assert "amix" not in graph and "atrim=start" not in graph
    with pytest.raises(ValueError):
        package_audio_cmd([], 0.0, 1600)


# ─── The .otioz ──────────────────────────────────────────────────────────


def _media(tmp_path):
    a = tmp_path / "Song_video.mp4"
    a.write_bytes(b"\x00" * 5000)
    b = tmp_path / "Song_take_t00000001.wav"
    b.write_bytes(bytes(range(256)) * 40)
    return [("media/Song_video.mp4", a), ("media/Song_take_t00000001.wav", b)]


def test_the_bundle_has_the_published_layout(tmp_path):
    media = _media(tmp_path)
    content = build_otio("x", 30, 10, CLIPS[:2])
    dest = tmp_path / "take" / "resolve.otioz"
    dest.parent.mkdir()
    progress = []
    write_otioz(dest, content, media, lambda: False, progress.append)
    with zipfile.ZipFile(dest) as zf:
        infos = zf.infolist()
        assert [i.filename for i in infos] == [
            "version.txt",
            "content.otio",
            "media/Song_video.mp4",
            "media/Song_take_t00000001.wav",
        ]
        assert [i.compress_type for i in infos] == [
            zipfile.ZIP_DEFLATED,
            zipfile.ZIP_DEFLATED,
            zipfile.ZIP_STORED,
            zipfile.ZIP_STORED,
        ]
        assert zf.read("version.txt") == b"1.0.0\n"
        assert zf.read("content.otio").decode() == content
        assert zf.read("media/Song_take_t00000001.wav") == media[1][1].read_bytes()
    assert progress[-1] == 1.0
    # Nothing left beside it but the package.
    assert [p.name for p in dest.parent.iterdir()] == ["resolve.otioz"]


def test_a_cancelled_bundle_leaves_nothing(tmp_path):
    media = _media(tmp_path)
    dest = tmp_path / "take" / "resolve.otioz"
    dest.parent.mkdir()
    with pytest.raises(RenderCancelled):
        write_otioz(dest, "{}", media, lambda: True)
    assert list(dest.parent.iterdir()) == []


def test_a_bundle_member_outside_media_is_refused(tmp_path):
    media = _media(tmp_path)
    with pytest.raises(ValueError):
        write_otioz(tmp_path / "r.otioz", "{}", [("../x.wav", media[0][1])], lambda: False)
    with pytest.raises(ValueError):
        write_otioz(tmp_path / "r.otioz", "{}", [("media/a/x.wav", media[0][1])], lambda: False)


# ─── Startup sweep ───────────────────────────────────────────────────────


def test_the_sweep_removes_exactly_the_package_leftovers(tmp_path):
    """Exact patterns only: a temp .otioz and a work folder go; the package,
    the user's own files with similar names and anything else stay."""
    take = tmp_path / "abcdefabcdef" / "takes" / "t0000aaaa"
    take.mkdir(parents=True)
    hexid = "0123456789abcdef0123456789abcdef"
    stale = take / f".resolve.otioz.{hexid}.tmp"
    stale.write_bytes(b"x")
    work = take / f".resolvework-{hexid}"
    (work / "sub").mkdir(parents=True)
    (work / "sub" / "a.wav").write_bytes(b"x")
    keep = [
        take / "resolve.otioz",
        take / ".resolve.otioz.notes.tmp",
        take / f".resolve.otioz.{hexid}.tmp.bak",
        take / f".resolve.otioz.{hexid[:-1]}.tmp",
        take / ".resolvework-notes.txt",
        take / "audio.wav",
    ]
    for path in keep:
        path.write_bytes(b"y")
    kept_dir = take / ".resolvework-mine"
    kept_dir.mkdir()
    # A file named like a work folder is not one, and a folder named like a
    # temp file is not one either.
    odd_file = take / f".resolvework-{hexid[::-1]}"
    odd_file.write_bytes(b"z")
    odd_dir = take / f".resolve.otioz.{hexid[::-1]}.tmp"
    odd_dir.mkdir()
    assert take_render.sweep_render_temps(tmp_path) == 2
    assert not stale.exists() and not work.exists()
    assert all(p.exists() for p in keep)
    assert kept_dir.is_dir() and odd_file.is_file() and odd_dir.is_dir()
