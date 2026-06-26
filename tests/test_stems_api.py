from __future__ import annotations

import json

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
    import app.api.stems as stems_mod

    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
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
