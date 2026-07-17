from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.api import stems as stems_mod

    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    # Isolate the mixdown render cache to a per-test dir -- these tests must
    # never read or pollute the developer's real cache.
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    from app.main import app

    return TestClient(app)


def _make_stem_file(tmp_path, job_id: str, name: str, contents: bytes = b"RIFF"):
    stems_dir = tmp_path / job_id / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)
    path = stems_dir / f"{name}.wav"
    path.write_bytes(contents)
    return path


def test_rejects_malformed_job_id(client):
    for bad_id in ("../etc", "ABC", "abcdefabcdef0", "abcdefabcde", "abcd-efabcdef"):
        r = client.get(f"/api/jobs/{bad_id}/stems/vocals.wav")
        assert r.status_code == 404, f"id {bad_id!r} should 404"


def test_rejects_unknown_stem_name(client):
    job = Job(id="abcdefabcdef")
    job.status = "done"
    _jobs[job.id] = job
    r = client.get(f"/api/jobs/{job.id}/stems/banjo.wav")
    assert r.status_code == 404


def test_requires_done_status(client, tmp_path):
    job = Job(id="abcdefabcdef")
    job.status = "separating"
    _jobs[job.id] = job
    _make_stem_file(tmp_path, job.id, "vocals")
    r = client.get(f"/api/jobs/{job.id}/stems/vocals.wav")
    assert r.status_code == 404


def test_serves_done_job_stem(client, tmp_path):
    job = Job(id="abcdefabcdee")
    job.status = "done"
    _jobs[job.id] = job
    _make_stem_file(tmp_path, job.id, "vocals", b"RIFF1234")
    r = client.get(f"/api/jobs/{job.id}/stems/vocals.wav")
    assert r.status_code == 200
    assert r.content == b"RIFF1234"
    assert r.headers["content-type"] == "audio/wav"


# --- peaks endpoint ---


def _make_peaks_file(tmp_path, job_id: str, data: dict) -> None:
    stems_dir = tmp_path / job_id / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)
    (stems_dir / "peaks.json").write_text(json.dumps(data), encoding="utf-8")


def test_peaks_returns_json_for_done_job(client, tmp_path):
    job = Job(id="abcdefabcdea")
    job.status = "done"
    _jobs[job.id] = job
    payload = {"vocals": [[-0.1, 0.2], [-0.3, 0.4]], "drums": [[-0.5, 0.6]]}
    _make_peaks_file(tmp_path, job.id, payload)

    r = client.get(f"/api/jobs/{job.id}/stems/peaks.json")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    assert "immutable" in r.headers.get("cache-control", "")
    assert r.json() == payload


def test_peaks_404_when_file_missing(client, tmp_path):
    job = Job(id="abcdefabcdeb")
    job.status = "done"
    _jobs[job.id] = job
    # stems dir exists but no peaks.json
    (tmp_path / job.id / "stems").mkdir(parents=True, exist_ok=True)

    r = client.get(f"/api/jobs/{job.id}/stems/peaks.json")
    assert r.status_code == 404


def test_peaks_404_for_non_done_job(client):
    job = Job(id="abcdefabcdec")
    job.status = "separating"
    _jobs[job.id] = job

    r = client.get(f"/api/jobs/{job.id}/stems/peaks.json")
    assert r.status_code == 404


def test_peaks_rejects_malformed_job_id(client):
    for bad_id in ("../etc", "ABC", "abcdefabcdef0", "abcdefabcde"):
        r = client.get(f"/api/jobs/{bad_id}/stems/peaks.json")
        assert r.status_code == 404, f"id {bad_id!r} should 404"


# ── Export All Stems (.zip) ──


def test_all_stems_zip_all_when_no_subset(client, tmp_path):
    import io
    import zipfile

    job = Job(id="abcdefabcdab")
    job.status = "done"
    job.title = "My Song! (Live)"
    _jobs[job.id] = job
    _make_stem_file(tmp_path, job.id, "vocals", b"RIFFvocals")
    _make_stem_file(tmp_path, job.id, "drums", b"RIFFdrums")
    _make_stem_file(tmp_path, job.id, "bass", b"RIFFbass")

    r = client.get(f"/api/jobs/{job.id}/stems/all.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "My_Song_Live_stems.zip" in r.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == ["bass.wav", "drums.wav", "vocals.wav"]
    assert zf.read("vocals.wav") == b"RIFFvocals"


def test_all_stems_zip_only_active_subset(client, tmp_path):
    """Only the requested (active) stems are bundled — not every stem on disk."""
    import io
    import zipfile

    job = Job(id="abcdefabcdba")
    job.status = "done"
    _jobs[job.id] = job
    for name in ("vocals", "drums", "bass", "guitar", "piano", "other"):
        _make_stem_file(tmp_path, job.id, name, f"RIFF{name}".encode())

    r = client.get(f"/api/jobs/{job.id}/stems/all.zip?stems=vocals,bass")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == ["bass.wav", "vocals.wav"]


def test_all_stems_zip_rejects_unknown_stem(client, tmp_path):
    job = Job(id="abcdefabcdbb")
    job.status = "done"
    _jobs[job.id] = job
    _make_stem_file(tmp_path, job.id, "vocals")
    r = client.get(f"/api/jobs/{job.id}/stems/all.zip?stems=vocals,banjo")
    assert r.status_code == 422


def test_all_stems_zip_rejects_bad_format(client, tmp_path):
    job = Job(id="abcdefabcdac")
    job.status = "done"
    _jobs[job.id] = job
    _make_stem_file(tmp_path, job.id, "vocals")
    r = client.get(f"/api/jobs/{job.id}/stems/all.zip?format=ogg")
    assert r.status_code == 422


def test_all_stems_zip_404_for_unknown_job(client):
    r = client.get("/api/jobs/abcdefabcdad/stems/all.zip")
    assert r.status_code == 404


def test_all_stems_zip_rejects_malformed_job_id(client):
    for bad_id in ("../etc", "ABC", "abcdefabcdef0", "abcdefabcde"):
        r = client.get(f"/api/jobs/{bad_id}/stems/all.zip")
        assert r.status_code == 404, f"id {bad_id!r} should 404"


def test_all_stems_zip_404_when_no_stem_files(client, tmp_path):
    job = Job(id="abcdefabcdae")
    job.status = "done"
    _jobs[job.id] = job
    (tmp_path / job.id / "stems").mkdir(parents=True, exist_ok=True)
    r = client.get(f"/api/jobs/{job.id}/stems/all.zip")
    assert r.status_code == 404


def test_all_stems_zip_mp3(client, tmp_path):
    """MP3 zip transcodes via ffmpeg; skip if ffmpeg isn't available."""
    import io
    import shutil
    import zipfile

    if shutil.which("ffmpeg") is None:
        import pytest

        pytest.skip("ffmpeg not available")

    # A real (tiny) WAV so ffmpeg can transcode it.
    import struct

    sr = 8000
    nframes = sr // 10
    data = b"\x00\x00" * nframes
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
    hdr += b"data" + struct.pack("<I", len(data))
    wav = hdr + data

    job = Job(id="abcdefabcdaf")
    job.status = "done"
    job.title = "Track"
    _jobs[job.id] = job
    _make_stem_file(tmp_path, job.id, "vocals", wav)

    r = client.get(f"/api/jobs/{job.id}/stems/all.zip?format=mp3")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.namelist() == ["vocals.mp3"]
    assert len(zf.read("vocals.mp3")) > 0


# --- dynamic mixdown endpoint (#183) ---


def _tiny_wav(seconds: float = 0.2, sr: int = 8000) -> bytes:
    """A minimal silent PCM16 mono WAV so ffmpeg can decode/mix it."""
    import struct

    nframes = int(sr * seconds)
    data = b"\x00\x00" * nframes
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
    hdr += b"data" + struct.pack("<I", len(data))
    return hdr + data


def _done_job_with_stems(tmp_path, job_id: str, names) -> Job:
    job = Job(id=job_id)
    job.status = "done"
    job.title = "Track"
    _jobs[job.id] = job
    for name in names:
        _make_stem_file(tmp_path, job_id, name, _tiny_wav())
    return job


def test_mixdown_rejects_bad_ext(client):
    r = client.get("/api/jobs/abcdef000001/mixdown.ogg?stems=vocals&gains=1")
    assert r.status_code == 404


def test_mixdown_rejects_length_mismatch(client):
    r = client.get("/api/jobs/abcdef000001/mixdown.wav?stems=vocals,drums&gains=1")
    assert r.status_code == 422


def test_mixdown_rejects_empty(client):
    r = client.get("/api/jobs/abcdef000001/mixdown.wav?stems=&gains=")
    assert r.status_code == 422


def test_mixdown_rejects_bad_gain(client):
    for gains in ("abc", "-1", "99"):
        r = client.get(f"/api/jobs/abcdef000001/mixdown.wav?stems=vocals&gains={gains}")
        assert r.status_code == 422, f"gains={gains!r} should 422"


def test_mixdown_rejects_unknown_stem(client):
    # "mix" is intentionally excluded (it is the static pre-render we replace).
    for stem in ("banjo", "mix"):
        r = client.get(f"/api/jobs/abcdef000001/mixdown.wav?stems={stem}&gains=1")
        assert r.status_code == 422, f"stem={stem!r} should 422"


def test_mixdown_rejects_bad_region(client):
    r = client.get("/api/jobs/abcdef000001/mixdown.wav?stems=vocals&gains=1&start=5&end=2")
    assert r.status_code == 422


def test_mixdown_rejects_malformed_job_id(client):
    r = client.get("/api/jobs/ZZZ/mixdown.wav?stems=vocals&gains=1")
    assert r.status_code == 404


def test_mixdown_requires_done(client, tmp_path):
    job = Job(id="abcdef000002")
    job.status = "separating"
    _jobs[job.id] = job
    _make_stem_file(tmp_path, job.id, "vocals", _tiny_wav())
    r = client.get(f"/api/jobs/{job.id}/mixdown.wav?stems=vocals&gains=1")
    assert r.status_code == 404


def test_mixdown_404_for_missing_stem_file(client, tmp_path):
    job = Job(id="abcdef000003")
    job.status = "done"
    _jobs[job.id] = job  # no stem files on disk
    r = client.get(f"/api/jobs/{job.id}/mixdown.wav?stems=vocals&gains=1")
    assert r.status_code == 404


def _skip_without_ffmpeg():
    import shutil

    if shutil.which("ffmpeg") is None:
        import pytest

        pytest.skip("ffmpeg not available")


def test_mixdown_wav_happy(client, tmp_path):
    _skip_without_ffmpeg()
    job = _done_job_with_stems(tmp_path, "abcdef000010", ["vocals", "drums"])
    r = client.get(f"/api/jobs/{job.id}/mixdown.wav?stems=vocals,drums&gains=1.000,0.500")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"


def test_mixdown_single_lane_skips_amix(client, tmp_path):
    _skip_without_ffmpeg()
    job = _done_job_with_stems(tmp_path, "abcdef000011", ["bass"])
    r = client.get(f"/api/jobs/{job.id}/mixdown.wav?stems=bass&gains=1.500")
    assert r.status_code == 200
    assert r.content[:4] == b"RIFF"


def test_mixdown_mp3_happy(client, tmp_path):
    _skip_without_ffmpeg()
    job = _done_job_with_stems(tmp_path, "abcdef000012", ["vocals", "bass"])
    r = client.get(f"/api/jobs/{job.id}/mixdown.mp3?stems=vocals,bass&gains=1,1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert len(r.content) > 0


def test_mixdown_region_trim(client, tmp_path):
    _skip_without_ffmpeg()
    job = _done_job_with_stems(tmp_path, "abcdef000013", ["vocals", "drums"])
    r = client.get(
        f"/api/jobs/{job.id}/mixdown.wav?stems=vocals,drums&gains=1,1&start=0.05&end=0.15"
    )
    assert r.status_code == 200
    assert r.content[:4] == b"RIFF"


def test_mixdown_flac_happy(client, tmp_path):
    _skip_without_ffmpeg()
    job = _done_job_with_stems(tmp_path, "abcdef000014", ["vocals", "bass"])
    r = client.get(f"/api/jobs/{job.id}/mixdown.flac?stems=vocals,bass&gains=1,1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/flac"
    assert r.content[:4] == b"fLaC"  # FLAC stream marker


def test_mixdown_honors_export_sample_rate(client, tmp_path, monkeypatch):
    # The exported WAV is resampled to the user's chosen export rate (issue: MPC
    # rejecting 44.1 kHz). The rate is read live from settings per request.
    _skip_without_ffmpeg()
    monkeypatch.setattr("app.api.stems.get_export_sample_rate", lambda: 48000)
    job = _done_job_with_stems(tmp_path, "abcdef000015", ["vocals"])
    r = client.get(f"/api/jobs/{job.id}/mixdown.wav?stems=vocals&gains=1")
    assert r.status_code == 200
    assert r.content[:4] == b"RIFF"
    # Canonical PCM WAV header: sample rate is the little-endian uint32 at byte 24.
    assert int.from_bytes(r.content[24:28], "little") == 48000


# ─── #290: mixdown render cache ────────────────────────────────────────────


def test_mixdown_cache_hit_skips_second_render(client, tmp_path, monkeypatch):
    _skip_without_ffmpeg()
    from app.api import stems as stems_mod

    calls = {"n": 0}
    original = stems_mod._stream_ffmpeg

    def counting_stream_ffmpeg(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(stems_mod, "_stream_ffmpeg", counting_stream_ffmpeg)

    job = _done_job_with_stems(tmp_path, "abcdef000020", ["vocals", "drums"])
    url = f"/api/jobs/{job.id}/mixdown.wav?stems=vocals,drums&gains=1,0.5"

    r1 = client.get(url)
    assert r1.status_code == 200
    assert calls["n"] == 1
    cache_files = list((tmp_path / "cache" / "mixdown").glob("*.wav"))
    assert len(cache_files) == 1

    r2 = client.get(url)
    assert r2.status_code == 200
    assert calls["n"] == 1  # no second render -- served from cache
    assert r2.content == r1.content


def test_mixdown_cache_key_varies_with_params(client, tmp_path):
    _skip_without_ffmpeg()
    job = _done_job_with_stems(tmp_path, "abcdef000021", ["vocals", "drums"])
    base = f"/api/jobs/{job.id}/mixdown.wav?stems=vocals,drums"

    urls = [
        f"{base}&gains=1,0.5",
        f"{base}&gains=1,0.9",  # different gains
        f"{base}&gains=1,0.5&start=0.05&end=0.15",  # region trim
    ]
    for url in urls:
        r = client.get(url)
        assert r.status_code == 200, url

    cache_files = list((tmp_path / "cache" / "mixdown").glob("*.wav"))
    assert len(cache_files) == 3  # three distinct cache entries, no collisions


def test_mixdown_failed_render_leaves_no_cache_entry(client, tmp_path):
    _skip_without_ffmpeg()
    job = _done_job_with_stems(tmp_path, "abcdef000022", ["vocals"])
    # Not a real WAV -- ffmpeg will exit non-zero trying to decode it.
    (tmp_path / job.id / "stems" / "vocals.wav").write_bytes(b"not audio data at all")

    r = client.get(f"/api/jobs/{job.id}/mixdown.wav?stems=vocals&gains=1")
    assert r.status_code == 200  # HTTP status is already committed mid-stream (#280)
    assert r.content == b""  # ffmpeg produced nothing before failing

    cache_dir = tmp_path / "cache" / "mixdown"
    leftover = list(cache_dir.glob("*")) if cache_dir.is_dir() else []
    assert leftover == [], "a failed render must not leave a cache entry or a stray temp file"


def test_prune_mixdown_cache_bounds_file_count(tmp_path, monkeypatch):
    from app.api import stems as stems_mod

    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_MAX_FILES", 3)
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_MAX_BYTES", 10**9)
    cache_dir = tmp_path / "mixdown"
    cache_dir.mkdir()
    for i in range(5):
        p = cache_dir / f"entry{i}.wav"
        p.write_bytes(b"x")
        os.utime(p, (i, i))  # oldest first: entry0 is oldest

    stems_mod._prune_mixdown_cache(cache_dir)

    remaining = {p.name for p in cache_dir.iterdir()}
    assert len(remaining) == 3
    assert remaining == {"entry2.wav", "entry3.wav", "entry4.wav"}  # newest 3 survive


def test_prune_mixdown_cache_bounds_total_size(tmp_path, monkeypatch):
    from app.api import stems as stems_mod

    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_MAX_FILES", 100)
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_MAX_BYTES", 25)
    cache_dir = tmp_path / "mixdown"
    cache_dir.mkdir()
    for i in range(5):
        p = cache_dir / f"entry{i}.wav"
        p.write_bytes(b"x" * 10)  # 5 * 10 = 50 bytes total, budget is 25
        os.utime(p, (i, i))

    stems_mod._prune_mixdown_cache(cache_dir)

    remaining = list(cache_dir.iterdir())
    assert sum(p.stat().st_size for p in remaining) <= 25


@pytest.mark.asyncio
async def test_stream_ffmpeg_logs_stderr_on_failure(caplog):
    """#280: a mid-stream ffmpeg failure can't change the HTTP status, so the
    stderr tail must land in the log -- previously it went to DEVNULL and a
    corrupt download left no trace anywhere."""
    import logging
    import sys

    from app.api.stems import _stream_ffmpeg

    cmd = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('partial-bytes'); sys.stdout.flush();"
        " sys.stderr.write('boom: encoder exploded\\n'); sys.exit(2)",
    ]
    with caplog.at_level(logging.WARNING, logger="stemdeck.api"):
        chunks = [c async for c in _stream_ffmpeg(cmd, context="mixdown job=test ext=wav")]

    assert b"".join(chunks) == b"partial-bytes"  # stream still delivered
    warning = next(r.message for r in caplog.records if "stream ffmpeg exit" in r.message)
    assert "mixdown job=test ext=wav" in warning
    assert "boom: encoder exploded" in warning


@pytest.mark.asyncio
async def test_stream_ffmpeg_clean_exit_logs_nothing(caplog):
    import logging
    import sys

    from app.api.stems import _stream_ffmpeg

    cmd = [sys.executable, "-c", "import sys; sys.stdout.write('ok')"]
    with caplog.at_level(logging.WARNING, logger="stemdeck.api"):
        chunks = [c async for c in _stream_ffmpeg(cmd, context="happy")]

    assert b"".join(chunks) == b"ok"
    assert not [r for r in caplog.records if "stream ffmpeg exit" in r.message]


def test_mixdown_rejects_unknown_ext_still(client):
    # ogg remains unsupported even after adding flac.
    r = client.get("/api/jobs/abcdef000001/mixdown.ogg?stems=vocals&gains=1")
    assert r.status_code == 404


def test_all_stems_zip_flac(client, tmp_path):
    _skip_without_ffmpeg()
    job = _done_job_with_stems(tmp_path, "abcdef000015", ["vocals"])
    job.title = "Track"
    import io
    import zipfile

    r = client.get(f"/api/jobs/{job.id}/stems/all.zip?format=flac")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.namelist() == ["vocals.flac"]
    assert zf.read("vocals.flac")[:4] == b"fLaC"


# --- MP4 video mux endpoint (#219) ---


def _make_video_file(tmp_path, job_id: str) -> None:
    """Generate a tiny real MP4 with a video stream at <job>/video.mp4 so the
    mux endpoint has something to stream-copy. Requires ffmpeg."""
    import subprocess

    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=0.3:r=10",
            "-c:v",
            "mpeg4",
            "-an",
            str(job_dir / "video.mp4"),
        ],
        check=True,
        timeout=30,
    )


def test_video_404_when_no_video_track(client, tmp_path):
    job = _done_job_with_stems(tmp_path, "abcdef000020", ["vocals"])
    r = client.get(f"/api/jobs/{job.id}/video.mp4?stems=vocals&gains=1")
    assert r.status_code == 404


def test_video_requires_done(client, tmp_path):
    job = Job(id="abcdef000021")
    job.status = "separating"
    _jobs[job.id] = job
    r = client.get(f"/api/jobs/{job.id}/video.mp4?stems=vocals&gains=1")
    assert r.status_code == 404


def test_video_rejects_malformed_job_id(client):
    r = client.get("/api/jobs/ZZZ/video.mp4?stems=vocals&gains=1")
    assert r.status_code == 404


def test_video_rejects_bad_params(client, tmp_path):
    _skip_without_ffmpeg()
    job = _done_job_with_stems(tmp_path, "abcdef000022", ["vocals", "drums"])
    _make_video_file(tmp_path, job.id)
    # length mismatch, bad gain, unknown stem all 422 once the video track exists.
    assert client.get(f"/api/jobs/{job.id}/video.mp4?stems=vocals,drums&gains=1").status_code == 422
    assert client.get(f"/api/jobs/{job.id}/video.mp4?stems=vocals&gains=99").status_code == 422
    assert client.get(f"/api/jobs/{job.id}/video.mp4?stems=mix&gains=1").status_code == 422


def test_video_mux_happy(client, tmp_path):
    _skip_without_ffmpeg()
    job = _done_job_with_stems(tmp_path, "abcdef000023", ["vocals", "drums"])
    _make_video_file(tmp_path, job.id)
    r = client.get(f"/api/jobs/{job.id}/video.mp4?stems=vocals,drums&gains=1.0,0.5")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    # ISO-BMFF: bytes 4-8 of the first box are the "ftyp" type.
    assert r.content[4:8] == b"ftyp"
