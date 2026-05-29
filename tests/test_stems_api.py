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
    r = client.get(f"/api/jobs/{job.id}/stems/all.zip?format=flac")
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
